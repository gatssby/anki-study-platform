from html.parser import HTMLParser
import html


VISUAL_WRAPPER_TAGS = {
    "span",
    "font",
    "b",
    "strong",
    "u",
    "s",
    "strike",
    "del",
    "mark",
}


class VisualWrapperStripper(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() in VISUAL_WRAPPER_TAGS:
            return
        self.parts.append(self._format_starttag(tag, attrs))

    def handle_startendtag(self, tag, attrs):
        if tag.lower() in VISUAL_WRAPPER_TAGS:
            return
        self.parts.append(self._format_starttag(tag, attrs, close=True))

    def handle_endtag(self, tag):
        if tag.lower() in VISUAL_WRAPPER_TAGS:
            return
        self.parts.append(f"</{tag}>")

    def handle_data(self, data):
        self.parts.append(data)

    def handle_entityref(self, name):
        self.parts.append(f"&{name};")

    def handle_charref(self, name):
        self.parts.append(f"&#{name};")

    def handle_comment(self, data):
        self.parts.append(f"<!--{data}-->")

    def handle_decl(self, decl):
        self.parts.append(f"<!{decl}>")

    def handle_pi(self, data):
        self.parts.append(f"<?{data}>")

    def unknown_decl(self, data):
        self.parts.append(f"<![{data}]>")

    def _format_starttag(self, tag, attrs, close=False):
        rendered_attrs = []
        for name, value in attrs:
            if value is None:
                rendered_attrs.append(html.escape(str(name), quote=True))
            else:
                rendered_attrs.append(
                    f'{html.escape(str(name), quote=True)}="{html.escape(str(value), quote=True)}"'
                )
        attr_text = f" {' '.join(rendered_attrs)}" if rendered_attrs else ""
        suffix = " />" if close else ">"
        return f"<{tag}{attr_text}{suffix}"


def strip_visual_wrappers(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("strip_visual_wrappers expects str")
    parser = VisualWrapperStripper()
    parser.feed(value)
    parser.close()
    return "".join(parser.parts)


class PlainTextExtractor(HTMLParser):
    BLOCK_TAGS = {"br", "div", "p", "li", "tr", "td", "th", "ul", "ol", "table"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_starttag(self, tag, _attrs):
        if tag.casefold() in self.BLOCK_TAGS:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag.casefold() in self.BLOCK_TAGS:
            self.parts.append(" ")

    def handle_data(self, data):
        self.parts.append(data)


def strip_html_text(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("strip_html_text expects str")
    parser = PlainTextExtractor()
    parser.feed(value)
    parser.close()
    return "".join(parser.parts)
