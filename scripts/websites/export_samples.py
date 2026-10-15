"""Generate six standalone example folders. Run from progressrooms-api."""
import argparse
import asyncio
import json
from pathlib import Path
from app.modules.websites.catalog import DESIGNS, sample_content
from app.modules.websites.schemas import WebsiteUpdatePayload
from app.modules.websites.rendering import render_document

async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', default='generated-websites/examples')
    parser.add_argument('--origin', default='http://localhost:3000')
    parser.add_argument('--name', default='Your Studio')
    parser.add_argument('--slug', default='your-studio')
    args = parser.parse_args()
    for number, design in enumerate(DESIGNS, 1):
        directory = Path(args.output) / f'template-{number}'
        directory.mkdir(parents=True, exist_ok=True)
        content = WebsiteUpdatePayload(**sample_content(design))
        html = await render_document(content,args.name,args.slug,args.origin)
        (directory/'index.html').write_text(html)
        (directory/'manifest.json').write_text(json.dumps({'template':design['code'],'format':'self-contained-html-v1'},indent=2))
        print(directory/'index.html')

if __name__ == '__main__':
    asyncio.run(main())
