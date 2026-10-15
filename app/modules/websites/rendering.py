"""Server-rendered, self-contained documents. No JavaScript or app runtime required."""
import base64
import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from string import Template
from urllib.parse import quote

import httpx
from fastapi import HTTPException
from app.core.config import settings
from .catalog import DESIGNS
from .schemas import WebsiteUpdatePayload, PRESET_PREFIX, IMAGE_PREFIX

MODULE_DIR = Path(__file__).parent
ASSET_DIR = MODULE_DIR / "assets"
TEMPLATE_DIR = MODULE_DIR / "templates"
MEDIA_DIR = Path(settings.WEBSITE_MEDIA_DIR)
EXPORT_DIR = Path(settings.WEBSITE_EXPORT_DIR)
MAX_IMAGE_BYTES = 5 * 1024 * 1024


def image_mime(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return None


async def embedded_image(url: str, org_id: str | None, media_dir: Path) -> str:
    if url.startswith(PRESET_PREFIX):
        path = ASSET_DIR / url.rsplit("/", 1)[-1]
        data = path.read_bytes()
    elif url.startswith(IMAGE_PREFIX):
        filename = url.rsplit("/", 1)[-1]
        # New uploads carry a tenant prefix. Older unprefixed image URLs remain valid.
        if len(filename.split(".")[0]) > 36 and not filename.startswith(f"{org_id}-"):
            raise HTTPException(403, "This image belongs to a different studio")
        path = media_dir / filename
        if not path.is_file():
            raise HTTPException(422, "An uploaded image is missing. Please upload it again before previewing or publishing.")
        data = path.read_bytes()
    else:
        # The schema restricts legacy downloads to https://images.unsplash.com/photo-*.
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=False, trust_env=False) as client:
                async with client.stream("GET", url) as response:
                    response.raise_for_status()
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > MAX_IMAGE_BYTES:
                            raise HTTPException(422, "An image is larger than 5 MB. Upload a smaller version.")
        except httpx.HTTPError as exc:
            raise HTTPException(422, "A legacy image could not be downloaded. Upload it or choose a sample photo.") from exc
    mime = image_mime(data)
    if not mime or len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(422, "Choose a PNG, JPEG, or WebP image smaller than 5 MB")
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


async def render_document(content: WebsiteUpdatePayload, name: str, slug: str, public_origin: str,
                          org_id: str | None = None, media_dir: Path | None = None) -> str:
    design = next(item for item in DESIGNS if item["code"] == content.template_code)
    classes_url = f"{public_origin.rstrip('/')}/{quote(slug, safe='')}/explore"
    canonical = f"{public_origin.rstrip('/')}/{quote(slug, safe='')}"
    # Restrained directional details only on the bolder poster and grid designs.
    cta_icon = '<span aria-hidden="true">→</span>' if design['layout'] in ('poster', 'grid') else ''
    button = f'<a class="button" href="{escape(classes_url, quote=True)}">Explore classes {cta_icon}</a>'
    images = {}
    cache = {}
    image_styles = []
    image_variables = {}
    for item in content.images:
        if item.url not in cache:
            cache[item.url] = await embedded_image(item.url, org_id, media_dir or MEDIA_DIR)
        if item.url not in image_variables:
            variable = f"--photo-{len(image_variables)}"
            image_variables[item.url] = variable
            image_styles.append(f"{variable}:url('{cache[item.url]}')")
        # Deduplicate bytes even when all 108 cards reuse the same photograph.
        images[item.id] = f'<div class="embedded-photo" role="img" aria-label="{escape(item.alt or "Studio photograph", quote=True)}" style="background-image:var({image_variables[item.url]})"></div>'
    photo_list = list(images.values())
    placeholder = '<div class="placeholder" aria-hidden="true">✳</div>'
    def photo(index):
        return photo_list[index % len(photo_list)] if photo_list else placeholder
    values = {key: escape(getattr(content, key)) for key in ("headline", "subheadline", "eyebrow")}
    values.update({"name": escape(name), "cta": button, **{f"image_{i}": photo(i) for i in range(3)}})
    hero = Template((TEMPLATE_DIR / f'{content.template_code.lower()}.html').read_text()).substitute(values)
    sections = []
    for index, section in enumerate(content.sections):
        cards = []
        for number, card in enumerate(section.cards):
            image = f'<div class="card-photo">{images[card.image_id]}</div>' if card.image_id else ''
            cards.append(f'''<article class="card">{image}<div class="card-body"><span class="card-number">{number+1:02d} /</span><h3>{escape(card.title)}</h3><p>{escape(card.text)}</p><a class="card-link" href="{escape(classes_url, quote=True)}">Explore classes</a></div></article>''')
        sections.append(f'''<section class="content-section" id="section-{escape(section.id, quote=True)}"><div class="section-heading"><div><p class="eyebrow">{index+2:02d} / Discover</p><h2>{escape(section.title)}</h2></div><p class="section-description">{escape(section.description)}</p></div><div class="cards">{''.join(cards)}</div></section>''')
    gallery = ''
    if len(photo_list) > 1:
        gallery = f'''<section class="gallery" aria-label="Studio photographs"><div class="gallery-heading"><h2 class="gallery-title">A glimpse inside.</h2><p class="eyebrow">A space for you</p></div><div class="gallery-photos">{''.join(f'<figure>{img}</figure>' for img in photo_list)}</div></section>'''
    sections_link = f'#section-{content.sections[0].id}' if content.sections else '#story'
    css = (TEMPLATE_DIR / 'site.css').read_text()
    title = escape(f"{name} — {content.headline}")
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{title}</title><meta name="description" content="{escape(content.subheadline, quote=True)}"><link rel="canonical" href="{escape(canonical, quote=True)}"><meta name="generator" content="ProgressRooms Website Studio"><style>:root{{--paper:{design['background']};--ink:{design['ink']};--accent:{design['accent']};{';'.join(image_styles)}}}{css}</style></head>
<body class="{design['layout']}"><a class="skip" href="#main-content">Skip to content</a><div class="site" id="top"><header class="header"><a class="brand" href="#top"><span class="brand-mark" aria-hidden="true">✳</span>{escape(name)}</a><nav class="nav" aria-label="Main navigation"><a class="nav-link" href="#story">Our story</a><a class="nav-link" href="{escape(sections_link, quote=True)}">Discover</a>{button}</nav></header><main id="main-content">{hero}<section class="story" id="story"><div class="story-picture">{photo(1)}</div><div class="story-copy"><p class="eyebrow">01 / Our story</p><h2>{escape(content.about_title)}</h2><p>{escape(content.about_text)}</p><a class="text-link" href="{escape(classes_url, quote=True)}">Find your next class</a></div></section>{''.join(sections)}{gallery}<section class="invitation"><p class="eyebrow">Take the next step</p><h2>{escape(content.cta_title)}</h2><p>{escape(content.cta_text)}</p>{button}</section></main><footer class="footer"><a class="brand" href="#top"><span class="brand-mark" aria-hidden="true">✳</span>{escape(name)}</a><span class="footer-note">© {datetime.now(timezone.utc).year} {escape(name)} · Made with ProgressRooms</span><a href="#top">Back to top ↑</a></footer></div></body></html>'''


def write_version(org_id: str, content: WebsiteUpdatePayload, html: str, kind: str) -> dict:
    """Only a successful DB commit makes a published version publicly discoverable."""
    assert kind in {"previews", "versions"}
    version = uuid.uuid4().hex
    directory = EXPORT_DIR / str(uuid.UUID(org_id)) / kind / version
    directory.mkdir(parents=True)
    data = html.encode('utf-8')
    (directory / 'index.html').write_bytes(data)
    manifest = {"version": version, "template_code": content.template_code, "created_at": datetime.now(timezone.utc).isoformat(), "sha256": hashlib.sha256(data).hexdigest(), "image_count": len(content.images), "section_count": len(content.sections), "format": "self-contained-html-v1"}
    (directory / 'manifest.json').write_text(json.dumps(manifest, indent=2))
    return manifest


def version_path(org_id: str, version: str, kind: str = "versions") -> Path:
    if not re.fullmatch(r"[a-f0-9]{32}", version) or kind not in {"versions", "previews"}:
        raise HTTPException(404, "Website version not found")
    path = EXPORT_DIR / str(uuid.UUID(org_id)) / kind / version / 'index.html'
    if not path.is_file():
        raise HTTPException(503, "The exported website is unavailable. Please restore its storage or publish again.")
    return path
