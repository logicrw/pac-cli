import asyncio

import httpx
import pytest

from bpc_fetch.extract import download_images


_IMAGE_BYTES = b"image" * 300


@pytest.mark.parametrize(
    "url",
    ["http://localhost/private.jpg", "http://10.0.0.8/private.jpg"],
)
def test_download_images_never_requests_private_initial_url(tmp_path, url):
    requested = []

    async def handler(request):
        requested.append(str(request.url))
        return httpx.Response(200, content=_IMAGE_BYTES)

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await download_images([url], tmp_path, client=client)

    assert asyncio.run(exercise()) == []
    assert requested == []


def test_download_images_never_requests_private_redirect_target(tmp_path):
    requested = []

    async def handler(request):
        requested.append(str(request.url))
        if request.url.host == "images.example":
            return httpx.Response(302, headers={"Location": "http://10.0.0.8/private.jpg"})
        return httpx.Response(200, content=_IMAGE_BYTES)

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=True
        ) as client:
            return await download_images(
                ["https://images.example/start.jpg"], tmp_path, client=client
            )

    assert asyncio.run(exercise()) == []
    assert requested == ["https://images.example/start.jpg"]


def test_download_images_follows_relative_public_redirect(tmp_path):
    requested = []

    async def handler(request):
        requested.append(str(request.url))
        if request.url.path == "/news/start.jpg":
            return httpx.Response(302, headers={"Location": "../assets/final.png"})
        return httpx.Response(200, content=_IMAGE_BYTES)

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await download_images(
                ["https://images.example/news/start.jpg"], tmp_path, client=client
            )

    saved = asyncio.run(exercise())
    assert requested == [
        "https://images.example/news/start.jpg",
        "https://images.example/assets/final.png",
    ]
    assert len(saved) == 1
    assert saved[0].read_bytes() == _IMAGE_BYTES


def test_download_images_saves_normal_image(tmp_path):
    requested = []

    async def handler(request):
        requested.append(str(request.url))
        return httpx.Response(200, content=_IMAGE_BYTES)

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await download_images(
                ["https://images.example/photo.webp"], tmp_path, client=client
            )

    saved = asyncio.run(exercise())
    assert requested == ["https://images.example/photo.webp"]
    assert len(saved) == 1
    assert saved[0].suffix == ".webp"
    assert saved[0].read_bytes() == _IMAGE_BYTES
