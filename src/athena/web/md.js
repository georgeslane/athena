// Markdown, as Athena writes it, made into the page's own elements: never HTML, so nothing it says can run or
// change the page. Paragraphs, headings, lists, quotes, code, bold, italics and links, which only go to web pages
// and email addresses.

import { h } from "./core.js";

export function markdown(text) {
  const out = document.createDocumentFragment();
  const lines = String(text || "").replace(/\r\n?/g, "\n").split("\n");
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) {
      i++;
      continue;
    }
    const fence = line.match(/^\s*(```|~~~)\s*([\w+-]*)\s*$/);
    if (fence) {
      const code = [];
      i++;
      while (i < lines.length && !lines[i].trim().startsWith(fence[1])) code.push(lines[i++]);
      i++; // the closing fence, if there is one
      out.append(h("pre.md-code", {}, h("code", { text: code.join("\n") })));
      continue;
    }
    const heading = line.match(/^\s*(#{1,6})\s+(.*?)\s*#*\s*$/);
    if (heading) {
      out.append(h(`p.md-heading.h${Math.min(heading[1].length, 3)}`, {}, inline(heading[2])));
      i++;
      continue;
    }
    if (/^\s*>/.test(line)) {
      const quoted = [];
      while (i < lines.length && /^\s*>/.test(lines[i])) quoted.push(lines[i++].replace(/^\s*>\s?/, ""));
      out.append(h("blockquote.md-quote", {}, markdown(quoted.join("\n"))));
      continue;
    }
    if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) {
      out.append(h("hr.md-rule"));
      i++;
      continue;
    }
    const item = listItem(line);
    if (item) {
      const list = h(item.ordered ? "ol.md-list" : "ul.md-list", item.ordered && item.start !== 1 ? { start: item.start } : {});
      while (i < lines.length) {
        const next = listItem(lines[i]);
        if (!next || next.ordered !== item.ordered) break;
        const body = [next.text];
        i++;
        // Lines indented under an item, or carrying it on, belong to it.
        while (i < lines.length && lines[i].trim() && !listItem(lines[i]) && /^\s+/.test(lines[i])) body.push(lines[i++].trim());
        list.append(h("li", {}, lineBreaks(body.join("\n"))));
      }
      out.append(list);
      continue;
    }
    const paragraph = [];
    while (i < lines.length && lines[i].trim() && !startsBlock(lines[i])) paragraph.push(lines[i++]);
    if (!paragraph.length) paragraph.push(lines[i++]); // a line that looked like a block but wasn't one
    out.append(h("p.md-p", {}, lineBreaks(paragraph.join("\n"))));
  }
  return out;
}

function listItem(line) {
  let m = line.match(/^\s{0,3}[-*+•]\s+(.*)$/);
  if (m) return { ordered: false, text: m[1] };
  m = line.match(/^\s{0,3}(\d{1,9})[.)]\s+(.*)$/);
  return m ? { ordered: true, start: Number(m[1]), text: m[2] } : null;
}

const startsBlock = (line) => /^\s*(```|~~~|#{1,6}\s|>)/.test(line) || Boolean(listItem(line));

function lineBreaks(text) {
  return text.split("\n").flatMap((part, i) => (i ? [h("br"), ...inline(part)] : inline(part)));
}

// Inline marks: `code`, **bold**, *italic* or _italic_, ~~struck~~, [links](https://…) and bare web addresses.
const INLINE = /(`+)([\s\S]*?[^`])\1(?!`)|\*\*([^*]+?)\*\*|__([^_]+?)__|(?<![\w*])\*(?!\s)([^*]+?)(?<!\s)\*(?![\w*])|(?<![\w_])_(?!\s)([^_]+?)(?<!\s)_(?![\w_])|~~([^~]+?)~~|\[([^\]]+)\]\(\s*([^)\s]+)\s*\)|(https?:\/\/[^\s<>"')\]]*[^\s<>"').,;:!?\]])/g;

export function inline(text) {
  const nodes = [];
  let at = 0;
  for (const m of text.matchAll(INLINE)) {
    if (m.index > at) nodes.push(text.slice(at, m.index));
    if (m[1]) nodes.push(h("code.md-inline", { text: m[2].trim() }));
    else if (m[3] || m[4]) nodes.push(h("strong", {}, inline(m[3] || m[4])));
    else if (m[5] || m[6]) nodes.push(h("em", {}, inline(m[5] || m[6])));
    else if (m[7]) nodes.push(h("s", {}, inline(m[7])));
    else if (m[8]) nodes.push(link(m[9], inline(m[8]), m[0]));
    else if (m[10]) nodes.push(link(m[10], [m[10]], m[0]));
    at = m.index + m[0].length;
  }
  if (at < text.length) nodes.push(text.slice(at));
  return nodes;
}

// A link, only to a web page or an email address: anything else stays as the text it was.
function link(href, children, original) {
  let url;
  try {
    url = new URL(href);
  } catch {
    return original;
  }
  if (!["http:", "https:", "mailto:"].includes(url.protocol)) return original;
  return h("a", { href: url.href, target: "_blank", rel: "noopener noreferrer" }, children);
}
