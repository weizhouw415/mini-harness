"use strict";

function escapeMarkdownHTML(text) {
  return text.replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
}

const markdownParser = new window.marked.Marked({
  gfm: true,
  breaks: true,
  renderer: {
    html({ text }) {
      return escapeMarkdownHTML(text);
    },
    image({ text }) {
      // Model text must not cause a browser request. Worker screenshots have their own renderer.
      return escapeMarkdownHTML(text || "图片");
    },
  },
});

const markdownSanitizerOptions = {
  ALLOWED_TAGS: [
    "a", "blockquote", "br", "code", "del", "em", "h1", "h2", "h3", "h4", "h5",
    "h6", "hr", "input", "li", "ol", "p", "pre", "strong", "table", "tbody", "td",
    "th", "thead", "tr", "ul",
  ],
  ALLOWED_ATTR: ["align", "checked", "disabled", "href", "start", "title", "type"],
  ALLOW_ARIA_ATTR: false,
  ALLOW_DATA_ATTR: false,
  RETURN_DOM_FRAGMENT: true,
};

function isMarkdownLink(href) {
  try {
    const { protocol } = new URL(href, document.baseURI);
    return ["http:", "https:", "mailto:"].includes(protocol);
  } catch {
    return false;
  }
}

function renderMarkdown(container, source) {
  container.classList.add("markdown");
  try {
    const html = markdownParser.parse(source);
    const fragment = window.DOMPurify.sanitize(html, markdownSanitizerOptions);
    for (const link of fragment.querySelectorAll("a[href]")) {
      if (isMarkdownLink(link.getAttribute("href"))) {
        link.target = "_blank";
        link.rel = "noopener noreferrer";
      } else {
        link.removeAttribute("href");
      }
    }
    container.replaceChildren(fragment);
  } catch {
    container.classList.remove("markdown");
    container.textContent = source;
  }
}
