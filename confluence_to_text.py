#!/usr/bin/env python3
"""confluence_raw*/*.html (storage format) -> 읽기 쉬운 텍스트로 변환 (큐레이션 작업용).
사용법: python3 confluence_to_text.py [src_dir] [out_dir]
src_dir 구조가 <src_dir>/*/*.html(앱/스페이스별 하위폴더) 또는 <src_dir>/*.html(평탄) 둘 다 지원."""
import os, re, sys, glob
from bs4 import BeautifulSoup

SRC_DIR = os.path.join(os.path.dirname(__file__), sys.argv[1] if len(sys.argv) > 1 else "confluence_raw")
OUT_DIR = os.path.join(os.path.dirname(__file__), sys.argv[2] if len(sys.argv) > 2 else "confluence_text")


def macro_name(tag):
    return tag.get("ac:name", "")


def render(soup):
    lines = []

    def walk(node, depth=0):
        for child in node.children:
            name = getattr(child, "name", None)
            if name is None:
                continue
            if name in ("h1", "h2", "h3", "h4", "h5", "h6"):
                level = int(name[1])
                lines.append("\n" + "#" * level + " " + child.get_text(" ", strip=True))
            elif name == "p":
                text = child.get_text(" ", strip=True)
                if text:
                    lines.append(text)
            elif name in ("ul", "ol"):
                for li in child.find_all("li", recursive=False):
                    lines.append("- " + li.get_text(" ", strip=True))
            elif name == "table":
                rows = child.find_all("tr")
                for r in rows:
                    cells = r.find_all(["th", "td"])
                    lines.append(" | ".join(c.get_text(" ", strip=True) for c in cells))
            elif name == "ac:structured-macro":
                mname = macro_name(child)
                if mname == "code":
                    code = child.find("ac:plain-text-body")
                    body = code.get_text("\n", strip=True) if code else child.get_text("\n", strip=True)
                    lines.append("```\n" + body + "\n```")
                elif mname in ("drawio", "gliffy", "image"):
                    lines.append(f"[다이어그램/이미지 macro: {mname} - 원본 페이지에서 확인 필요]")
                elif mname == "toc":
                    pass
                elif mname in ("info", "note", "warning", "tip"):
                    body = child.find("ac:rich-text-body")
                    text = body.get_text(" ", strip=True) if body else child.get_text(" ", strip=True)
                    lines.append(f"> [{mname}] {text}")
                else:
                    text = child.get_text(" ", strip=True)
                    if text:
                        lines.append(f"[macro:{mname}] {text}")
            elif name == "ac:layout" or name == "ac:layout-section" or name == "ac:layout-cell":
                walk(child, depth + 1)
            else:
                walk(child, depth + 1)

    walk(soup)
    return "\n".join(lines)


def convert_one(fpath, out_dir):
    with open(fpath, encoding="utf-8") as f:
        raw = f.read()
    meta = {}
    for m in re.finditer(r"<!--\s*(\w+):\s*(.*?)\s*-->", raw):
        meta[m.group(1)] = m.group(2)
    soup = BeautifulSoup(raw, "html.parser")
    text = render(soup)
    base = os.path.splitext(os.path.basename(fpath))[0]
    out_path = os.path.join(out_dir, base + ".txt")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(f"제목: {meta.get('title','')}\nURL: {meta.get('url','')}\n갱신: {meta.get('updated','')}\n\n")
        f.write(text)


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    subdirs = [d for d in sorted(glob.glob(os.path.join(SRC_DIR, "*"))) if os.path.isdir(d)]
    if subdirs:
        for app_dir in subdirs:
            app = os.path.basename(app_dir)
            out_app_dir = os.path.join(OUT_DIR, app)
            os.makedirs(out_app_dir, exist_ok=True)
            for fpath in sorted(glob.glob(os.path.join(app_dir, "*.html"))):
                convert_one(fpath, out_app_dir)
    else:
        for fpath in sorted(glob.glob(os.path.join(SRC_DIR, "*.html"))):
            convert_one(fpath, OUT_DIR)
    print("변환 완료 ->", OUT_DIR)


if __name__ == "__main__":
    main()
