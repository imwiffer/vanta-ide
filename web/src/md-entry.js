import { marked } from 'marked';
import DOMPurify from 'dompurify';

marked.setOptions({ breaks: true, gfm: true });

self.vantaMd = { render: (src) => DOMPurify.sanitize(marked.parse(src || '')) };
