"use strict";
// Markdown, the part models actually write.
//
// Measured on the 44 archived sessions: 50 % of assistant messages carry
// Markdown -- inline code 41 %, bold 23 %, ordered lists 13 %, headings 9 %,
// bullets 8 %, fences 3 %, tables 2 %, quotes 0 %. This covers that head and
// stops there; anything unsupported must survive as literal text rather than
// disappear.
//
// mdParse is PURE (string -> block tree, no DOM) so the pytest suite can drive
// it through node. Rendering lives in app.js and builds elements, never
// innerHTML: this text comes from a model and from files on disk.

const _FENCE = /^```(\S*)\s*$/;
const _HEADING = /^(#{1,6})\s+(.*)$/;
const _BULLET = /^\s*[-*]\s+(.*)$/;
const _ORDERED = /^\s*\d+\.\s+(.*)$/;

// Inline: code first -- inside a code span, ** is two asterisks, not bold.
function mdInline(text) {
  const spans = [];
  let plain = "";
  const flush = () => {
    if (plain) { spans.push({ t: "text", v: plain }); plain = ""; }
  };
  for (let i = 0; i < text.length;) {
    if (text[i] === "`") {
      const end = text.indexOf("`", i + 1);
      if (end > i + 1) {
        flush();
        spans.push({ t: "code", v: text.slice(i + 1, end) });
        i = end + 1;
        continue;
      }
    } else if (text.startsWith("**", i)) {
      const end = text.indexOf("**", i + 2);
      if (end > i + 2) {
        flush();
        spans.push({ t: "strong", v: text.slice(i + 2, end) });
        i = end + 2;
        continue;
      }
    }
    plain += text[i];
    i += 1;
  }
  flush();
  return spans;
}

function mdParse(src) {
  const lines = String(src == null ? "" : src).split("\n");
  const blocks = [];
  let para = [];
  let list = null;

  const closePara = () => {
    if (para.length) {
      blocks.push({ type: "para", spans: mdInline(para.join("\n")) });
      para = [];
    }
  };
  const closeList = () => {
    if (list) { blocks.push(list); list = null; }
  };
  const close = () => { closePara(); closeList(); };

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const fence = _FENCE.exec(line);
    if (fence) {
      close();
      const body = [];
      i += 1;
      // An unclosed fence runs to the end: a reply cut by the context window
      // still shows its code instead of three backticks in a paragraph.
      for (; i < lines.length && !_FENCE.test(lines[i]); i++) body.push(lines[i]);
      blocks.push({ type: "code", lang: fence[1], text: body.join("\n") });
      continue;
    }
    if (!line.trim()) { close(); continue; }

    const heading = _HEADING.exec(line);
    if (heading) {
      close();
      blocks.push({ type: "heading", level: heading[1].length,
                    spans: mdInline(heading[2]) });
      continue;
    }

    const bullet = _BULLET.exec(line);
    const ordered = _ORDERED.exec(line);
    if (bullet || ordered) {
      closePara();
      const isOrdered = Boolean(ordered);
      if (list && list.ordered !== isOrdered) closeList();
      if (!list) list = { type: "list", ordered: isOrdered, items: [] };
      list.items.push(mdInline((ordered || bullet)[1]));
      continue;
    }

    closeList();
    para.push(line);
  }
  close();
  return blocks;
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { mdParse, mdInline };  // for the pytest harness only
}
