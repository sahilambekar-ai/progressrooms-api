"""HTTP compatibility and abuse regressions for the security dependency upgrade."""
import httpx
import pytest
from fastapi import FastAPI, Request
from starlette.responses import FileResponse

from app.core.security import create_access_token, decode_access_token


@pytest.mark.asyncio
async def test_file_response_range_and_excessive_ranges(tmp_path):
    asset = tmp_path / 'asset.txt'
    asset.write_bytes(b'0123456789')
    probe = FastAPI()

    @probe.get('/asset')
    async def file():
        return FileResponse(asset)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=probe), base_url='http://test') as client:
        partial = await client.get('/asset', headers={'Range': 'bytes=2-5'})
        assert partial.status_code == 206
        assert partial.content == b'2345'
        assert partial.headers['content-range'] == 'bytes 2-5/10'
        excessive = await client.get('/asset', headers={'Range': 'bytes=' + ','.join(['0-1'] * 10000)})
        # Patched Starlette ignores excessive ranges and sends the bounded file.
        assert excessive.status_code == 200
        assert excessive.content == asset.read_bytes()


@pytest.mark.asyncio
async def test_multipart_accepts_files_and_bounds_non_file_parts():
    probe = FastAPI()

    @probe.post('/upload')
    async def upload(request: Request):
        async with request.form() as form:
            return {'name': form['file'].filename, 'caption': form['caption']}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=probe), base_url='http://test') as client:
        normal = await client.post('/upload', files={'file': ('safe.txt', b'ok')}, data={'caption': 'hello'})
        assert normal.status_code == 200
        assert normal.json() == {'name': 'safe.txt', 'caption': 'hello'}
        oversized = await client.post('/upload', files={'file': ('safe.txt', b'ok')}, data={'caption': 'x' * (1024 * 1024 + 1)})
        assert oversized.status_code == 400
        assert 'maximum size' in oversized.text


def test_jwt_upgrade_preserves_valid_tokens_and_rejects_tampering():
    token = create_access_token({'sub': 'dependency-probe'})
    assert decode_access_token(token)['sub'] == 'dependency-probe'
    head, payload, signature = token.split('.')
    signature = ('A' if signature[0] != 'A' else 'B') + signature[1:]
    assert decode_access_token(f'{head}.{payload}.{signature}') is None
    assert decode_access_token('not.a.jwt') is None
