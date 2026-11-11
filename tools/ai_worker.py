"""
Chat worker for the Vanta IDE.

Spawned by ide/server.py with the project's .venv interpreter. Speaks JSON lines:

  in : {"cmd":"load","model":"criscar22/Qwen2.5-Coder-7B-Instruct-abliterated",
        "compression":null}
       {"cmd":"chat","messages":[{"role":"user","content":"hi"}],
        "max_new_tokens":512,"temperature":0.7}
       {"cmd":"reset"}

  out: {"type":"state","msg":"loading|ready|generating|idle"}
       {"type":"model","msg":"..."}
       {"type":"note","msg":"engine + precision chosen"}
       {"type":"delta","text":"partial"}
       {"type":"reply","text":"..."}
       {"type":"error","msg":"..."}
       {"type":"log","msg":"..."}

Engine and precision are chosen per checkpoint before any weight is touched
(see plan_load):

  * direct fp16  — weights fit in free VRAM. Loaded once, tokens stream through
                   a TextIteratorStreamer, generation is GPU-fast.
  * direct 4-bit — fp16 does not fit but nf4 does (needs bitsandbytes). Same
                   speed class, ~3.5x smaller resident weights.
  * airllm       — bigger than VRAM even at 4-bit; one layer resident at a
                   time, sharded to disk. Works far past VRAM but pays a
                   per-token layer-streaming tax, so it is the fallback.

`compression: "4bit"/"8bit"` forces that precision; `null` means auto.
Temperature drives sampling (do_sample/top_p/top_k/repetition_penalty); 0
falls back to greedy decoding.
"""

from __future__ import annotations

import gc
import json
import os
import re
import sys
import threading
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agent_tools  # noqa: E402

STATE = {"model": None, "name": None, "compression": None, "engine": None,
         "want": None, "want_comp": None}
MODEL = None
TOKENIZER = None
ENGINE = None
SIZE_CACHE: dict[str, int] = {}


def emit(**obj):
    # ensure_ascii: the parent may read this pipe under any code page — keep the
    # wire bytes 7-bit and let json.loads rebuild the characters.
    sys.stdout.write(json.dumps(obj, ensure_ascii=True) + "\n")
    sys.stdout.flush()


def state(msg: str):
    emit(type="state", msg=msg)


def note(msg: str):
    emit(type="note", msg=msg)


def log(msg: str):
    emit(type="log", msg=msg)


# --------------------------------------------------------------------------- #
# engine selection
# --------------------------------------------------------------------------- #

def free_vram() -> int:
    try:
        import torch
        if torch.cuda.is_available():
            return int(torch.cuda.mem_get_info()[0])
    except Exception:  # noqa: BLE001
        pass
    return 0


def weights_bytes(model_id: str) -> int:
    """Checkpoint size on disk (bytes), from the Hub index or the local cache."""
    if model_id in SIZE_CACHE:
        return SIZE_CACHE[model_id]
    total = 0
    if os.path.isdir(model_id):
        for root, _dirs, files in os.walk(model_id):
            for name in files:
                if name.endswith((".safetensors", ".bin")):
                    total += os.path.getsize(os.path.join(root, name))
        SIZE_CACHE[model_id] = total
        return total
    try:
        from huggingface_hub import HfApi
        info = HfApi().model_info(model_id, files_metadata=True)
        for sib in info.siblings or []:
            name = getattr(sib, "rfilename", "") or ""
            if name.endswith((".safetensors", ".bin")) and getattr(sib, "size", None):
                total += int(sib.size)
    except Exception:  # noqa: BLE001
        total = 0
    if not total:
        try:
            from huggingface_hub import snapshot_download
            local = snapshot_download(model_id, local_files_only=True)
            for root, _dirs, files in os.walk(local):
                for name in files:
                    if name.endswith((".safetensors", ".bin")):
                        total += os.path.getsize(os.path.join(root, name))
        except Exception:  # noqa: BLE001
            total = 0
    SIZE_CACHE[model_id] = total
    return total


def bnb_available() -> bool:
    try:
        import bitsandbytes  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def plan_load(model_id: str, requested: str | None) -> tuple[str, str | None, str]:
    """Choose engine and effective precision before any weight is touched.

    Returns (engine, compression, note). With no explicit compression the
    checkpoint is loaded at fp16 when it fits, else 4-bit nf4 when that fits,
    else AirLLM streams layers from disk.
    """
    need = weights_bytes(model_id)
    free = free_vram()
    bnb = bnb_available()

    if not need or not free:
        why = "checkpoint size unknown" if not need else "no CUDA device"
        return "airllm", (requested or None), f"AirLLM layer streaming · {why}"

    if requested in ("4bit", "8bit"):
        est = need / (3.5 if requested == "4bit" else 2.0)
        if bnb and est * 1.15 < free:
            return ("direct", requested,
                    f"direct {requested} GPU load · ~{est / 1e9:.1f} GB of "
                    f"{free / 1e9:.1f} GB free VRAM")
        return ("airllm", requested,
                f"AirLLM layer streaming · {requested} forced, "
                f"~{est / 1e9:.1f} GB does not fit {free / 1e9:.1f} GB free VRAM")

    if need * 1.15 < free:
        return ("direct", None,
                f"direct fp16 GPU load · {need / 1e9:.1f} GB fits in "
                f"{free / 1e9:.1f} GB free VRAM · fast path")

    if bnb:
        est = need / 3.5
        if est * 1.15 < free:
            return ("direct", "4bit",
                    f"direct 4-bit nf4 · fp16 needs {need / 1e9:.1f} GB > "
                    f"{free / 1e9:.1f} GB free VRAM, quantised ~{est / 1e9:.1f} GB fits")

    return ("airllm", None,
            f"AirLLM layer streaming · {need / 1e9:.1f} GB exceeds "
            f"{free / 1e9:.1f} GB free VRAM even quantised")


# --------------------------------------------------------------------------- #
# chat templates
# --------------------------------------------------------------------------- #

def normalize_messages(messages: list[dict]) -> list[dict]:
    """Merge leading system messages into one and ensure alternating/valid roles
    so strict Jinja templates (Mistral, Llama-3, DeepSeek, Qwen) never fail."""
    sys_parts: list[str] = []
    rest: list[dict] = []
    in_leading_sys = True
    for m in messages:
        role = m.get("role", "user")
        content = (m.get("content") or "").strip()
        if not content:
            continue
        if role == "system" and in_leading_sys:
            sys_parts.append(content)
        else:
            in_leading_sys = False
            eff_role = "user" if role == "system" else role
            if rest and rest[-1]["role"] == eff_role:
                rest[-1]["content"] += "\n\n" + content
            else:
                rest.append({"role": eff_role, "content": content})
    out: list[dict] = []
    if sys_parts:
        out.append({"role": "system", "content": "\n\n".join(sys_parts)})
    out.extend(rest)
    return out


def apply_template(messages: list[dict], **kwargs):
    """Qwen3 and friends accept enable_thinking — small models get lost in it."""
    tok = TOKENIZER
    norm = normalize_messages(messages)
    for extra in ({"enable_thinking": False}, {}):
        try:
            return tok.apply_chat_template(norm, add_generation_prompt=True, **extra, **kwargs)
        except TypeError:
            continue
        except Exception:
            break
    # Fallback if template rejects system role entirely (e.g. Gemma/older Mistral)
    if norm and norm[0]["role"] == "system":
        sys_txt = norm[0]["content"]
        merged = list(norm[1:])
        if merged and merged[0]["role"] == "user":
            merged[0] = {"role": "user", "content": sys_txt + "\n\n" + merged[0]["content"]}
        else:
            merged.insert(0, {"role": "user", "content": sys_txt})
        return tok.apply_chat_template(merged, add_generation_prompt=True, **kwargs)
    return tok.apply_chat_template(norm, add_generation_prompt=True, **kwargs)


def build_prompt(messages: list[dict]) -> str:
    tok = TOKENIZER
    if tok is not None and getattr(tok, "chat_template", None):
        try:
            return apply_template(messages, tokenize=False)
        except Exception as exc:  # noqa: BLE001
            log(f"chat template failed: {exc}")
    parts = []
    for m in normalize_messages(messages):
        role = m.get("role", "user")
        content = m.get("content", "")
        if role == "system":
            parts.append(f"System: {content}")
        elif role == "assistant":
            parts.append(f"Assistant: {content}")
        else:
            parts.append(f"User: {content}")
    parts.append("Assistant:")
    return "\n\n".join(parts)


# --------------------------------------------------------------------------- #
# load paths
# --------------------------------------------------------------------------- #

def load_direct(model_id: str, compression: str | None):
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    kwargs: dict = {"device_map": "cuda", "low_cpu_mem_usage": True}
    if compression in ("4bit", "8bit"):
        from transformers import BitsAndBytesConfig
        kwargs["quantization_config"] = BitsAndBytesConfig(
            load_in_4bit=compression == "4bit",
            load_in_8bit=compression == "8bit",
            bnb_4bit_compute_dtype=torch.float16,
            bnb_4bit_quant_type="nf4",
        )
    else:
        kwargs["dtype"] = torch.float16
    tok = AutoTokenizer.from_pretrained(model_id)
    model = AutoModelForCausalLM.from_pretrained(model_id, **kwargs)
    return model, tok


def load_airllm(model_id: str, compression: str | None):
    from airllm import AutoModel
    kwargs = {"compression": compression} if compression else {}
    model = AutoModel.from_pretrained(model_id, **kwargs)
    return model, model.tokenizer


def load(model_id: str, compression: str | None):
    global MODEL, TOKENIZER, ENGINE
    state("loading")
    log(f"loading {model_id} (compression={compression or 'auto'})")

    engine, used, reason = plan_load(model_id, compression)
    note(reason)
    if engine == "direct":
        try:
            MODEL, TOKENIZER = load_direct(model_id, used)
            ENGINE = "direct"
        except Exception as exc:  # noqa: BLE001
            log(f"direct load failed ({exc}) — falling back to AirLLM")
            MODEL = TOKENIZER = None
            gc.collect()
            try:
                import torch
                torch.cuda.empty_cache()
            except Exception:  # noqa: BLE001
                pass
            note("direct load failed · switching to AirLLM layer streaming")
            MODEL, TOKENIZER = load_airllm(model_id, used)
            ENGINE = "airllm"
    else:
        MODEL, TOKENIZER = load_airllm(model_id, used)
        ENGINE = "airllm"

    STATE.update(model=model_id, name=model_id, compression=used, engine=ENGINE)
    emit(type="model", msg=model_id)
    log(f"device: {getattr(MODEL, 'device', 'n/a')} engine={ENGINE} precision={used or 'fp16'}")
    state("ready")


# --------------------------------------------------------------------------- #
# generation
# --------------------------------------------------------------------------- #

def chat_direct(messages: list[dict], max_new_tokens: int, temperature: float = 0.7) -> str:
    from transformers import TextIteratorStreamer

    enc = apply_template(messages, return_tensors="pt", return_dict=True,
                         truncation=True, max_length=16384)
    dev = getattr(MODEL, "device", "cuda") if hasattr(MODEL, "device") else "cuda"
    if isinstance(enc, dict):
        enc = {k: v.to(dev) for k, v in enc.items()}
    elif hasattr(enc, "to"):
        enc = {"input_ids": enc.to(dev)}
    else:
        enc = {"input_ids": enc}
    streamer = TextIteratorStreamer(TOKENIZER, skip_prompt=True,
                                    skip_special_tokens=True, timeout=600)
    kwargs = dict(enc, max_new_tokens=max_new_tokens, streamer=streamer)
    if temperature and temperature > 0.01:
        # greedy decoding makes a small model repeat itself word for word —
        # conversation needs sampling.
        kwargs.update(do_sample=True, temperature=float(temperature), top_p=0.95,
                      top_k=40, repetition_penalty=1.05)
    else:
        kwargs.update(do_sample=False)
    error: list[BaseException] = []

    def run():
        try:
            MODEL.generate(**kwargs)
        except BaseException as exc:  # noqa: BLE001
            error.append(exc)
            streamer.end()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    parts: list[str] = []
    for piece in streamer:
        if piece:
            parts.append(piece)
            emit(type="delta", text=piece)
    thread.join(timeout=30)
    if error:
        raise error[0]
    return "".join(parts).strip()


def chat_airllm(messages: list[dict], max_new_tokens: int, temperature: float = 0.7) -> str:
    prompt = build_prompt(messages)
    tokens = TOKENIZER([prompt], return_tensors="pt", truncation=True,
                       max_length=4096, padding=False)
    device = getattr(MODEL, "device", "cpu")
    input_ids = tokens["input_ids"].to(device)
    gen: dict = {"max_new_tokens": max_new_tokens, "use_cache": True,
                 "return_dict_in_generate": True}
    if temperature and temperature > 0.01:
        gen.update(do_sample=True, temperature=float(temperature), top_p=0.95)
    out = MODEL.generate(input_ids, **gen)
    new_tokens = out.sequences[0][input_ids.shape[-1]:]
    return TOKENIZER.decode(new_tokens, skip_special_tokens=True).strip()


def generate(messages: list[dict], max_new_tokens: int, temperature: float) -> str:
    if ENGINE == "direct":
        return chat_direct(messages, max_new_tokens, temperature)
    return chat_airllm(messages, max_new_tokens, temperature)


def _recover_oom() -> None:
    global MODEL, TOKENIZER, ENGINE
    gc.collect()
    try:
        import torch
        torch.cuda.empty_cache()
    except Exception:  # noqa: BLE001
        pass


def chat(messages: list[dict], max_new_tokens: int, temperature: float = 0.7,
         root: str = "", agent_enabled: bool = True):
    if MODEL is None:
        emit(type="error", msg="no model loaded - pick one and press Load")
        state("idle")
        return
    state("generating")
    started = time.time()
    convo = list(messages)
    ws = root if (root and os.path.isdir(root)) else os.getcwd()
    max_rounds = agent_tools.MAX_AGENT_STEPS if agent_enabled else 0

    for attempt in range(max_rounds + 1):
        try:
            text = generate(convo, max_new_tokens, temperature)
        except Exception as exc:  # noqa: BLE001
            if "out of memory" not in str(exc).lower():
                raise
            _recover_oom()
            note("GPU out of memory - freed the cache and retried once at half length")
            try:
                text = generate(convo, max(32, max_new_tokens // 2), temperature)
            except Exception as exc2:  # noqa: BLE001
                emit(type="error",
                     msg="GPU out of memory: another process is holding VRAM. Close it "
                         "(browser tab with a 3D page, game, another model), then press "
                         "Load. " + str(exc2).splitlines()[0][:200])
                state("ready")
                return

        calls = agent_tools.parse_tool_calls(text) if attempt < max_rounds else []
        if not calls:
            emit(type="reply", text=text)
            break

        # Finalize the current streaming step bubble before emitting tool events
        emit(type="step", text=text)
        observations: list[str] = []
        for call in calls:
            obs = agent_tools.execute_tool(
                call,
                ws,
                emit_fn=emit,
                on_file_change=lambda p: emit(type="file_changed", path=p),
            )
            observations.append(obs)

        obs_block = (
            "[Observation]\n"
            + "\n\n".join(observations)
            + "\n\nContinue based on the observation above. Emit another ACTION block if "
              "more steps are needed, or provide your final answer directly."
        )
        convo = convo + [
            {"role": "assistant", "content": text},
            {"role": "user", "content": obs_block},
        ]

    state("ready")
    log(f"generated in {time.time() - started:.1f}s engine={ENGINE}")


def reset():
    global MODEL, TOKENIZER, ENGINE
    MODEL = TOKENIZER = None
    ENGINE = None
    STATE.update(model=None, compression=None, engine=None)
    gc.collect()
    try:
        import torch
        torch.cuda.empty_cache()
    except Exception:  # noqa: BLE001
        pass
    state("idle")


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    state("idle")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        cmd = msg.get("cmd")
        try:
            if cmd == "load":
                model = msg.get("model") or ""
                if not model:
                    emit(type="error", msg="model id required")
                    state("idle")
                    continue
                if (model == STATE["model"] and MODEL is not None
                        and STATE["want_comp"] == (msg.get("compression") or None)):
                    state("ready")
                    continue
                STATE["want"] = model
                STATE["want_comp"] = msg.get("compression") or None
                load(model, msg.get("compression"))
            elif cmd == "chat":
                # self-heal: a fresh worker that only got the chat (server
                # restart, crashed worker) loads the last requested model first.
                if MODEL is None and STATE.get("want"):
                    note("worker was cold - loading " + str(STATE["want"]))
                    load(STATE["want"], STATE.get("want_comp"))
                chat(msg.get("messages", []), int(msg.get("max_new_tokens") or 256),
                     float(msg.get("temperature") if msg.get("temperature") is not None
                           else 0.7),
                     str(msg.get("workspaceRoot") or ""),
                     bool(msg.get("agentTools", True)))
            elif cmd == "reset":
                reset()
            else:
                emit(type="error", msg=f"unknown cmd {cmd}")
        except Exception as exc:  # noqa: BLE001
            emit(type="error", msg=f"{type(exc).__name__}: {exc}")
            log(traceback.format_exc(limit=3))
            state("idle")


if __name__ == "__main__":
    main()
