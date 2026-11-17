"""Example Vanta plugin — adds /api/ext/hello and greets on project open."""


def register(api):
    count = api.state.setdefault("opens", 0)

    def hello(body, query):
        api.save_state()
        return {"ok": True, "hello": "world", "opens": api.state["opens"]}

    api.route("hello", hello, methods=("GET", "POST"))

    def on_open(data):
        api.state["opens"] = api.state.get("opens", 0) + 1
        api.log(f"project opened: {data.get('root')}")
        api.save_state()

    api.on("project_open", on_open)
