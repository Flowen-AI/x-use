"""The legacy publisher must not convert requested media posts into text posts."""
from unittest.mock import AsyncMock, MagicMock

import pytest

import xuse.features.publisher.orchestrator as publisher
from xuse.models import AccountConfig, TweetContent


@pytest.mark.asyncio
@pytest.mark.parametrize('prepared', [[], ['only-one.png']])
async def test_partial_media_preparation_aborts_before_composer(monkeypatch, prepared):
    instance = object.__new__(publisher.TweetPublisher)
    instance.browser_manager = MagicMock()
    instance.account_config = AccountConfig(account_id='test')
    instance.llm_service = MagicMock()
    instance.media_dir = 'unused'
    monkeypatch.setattr(publisher, 'generate_post_text_if_needed', AsyncMock(return_value='post'))
    monkeypatch.setattr(publisher, 'prepare_media_paths', AsyncMock(return_value=prepared))
    compose = MagicMock(return_value=True)
    monkeypatch.setattr(publisher, 'compose_and_post', compose)
    content = TweetContent(text='post', local_media_paths=['one.png'], media_urls=['https://example.com/two.png'])
    assert await instance.post_new_tweet(content) is False
    compose.assert_not_called()


@pytest.mark.asyncio
async def test_complete_media_preparation_reaches_composer(monkeypatch):
    instance = object.__new__(publisher.TweetPublisher)
    instance.browser_manager = MagicMock()
    instance.account_config = AccountConfig(account_id='test')
    instance.llm_service = MagicMock()
    instance.media_dir = 'unused'
    monkeypatch.setattr(publisher, 'generate_post_text_if_needed', AsyncMock(return_value='post'))
    monkeypatch.setattr(publisher, 'prepare_media_paths', AsyncMock(return_value=['one.png', 'two.png']))
    compose = MagicMock(return_value=True)
    monkeypatch.setattr(publisher, 'compose_and_post', compose)
    content = TweetContent(text='post', local_media_paths=['one.png'], media_urls=['https://example.com/two.png'])
    assert await instance.post_new_tweet(content) is True
    assert compose.call_args.args[-1] == ['one.png', 'two.png']


@pytest.mark.asyncio
async def test_real_preparation_dropping_missing_file_and_failed_download_aborts(monkeypatch, tmp_path):
    import xuse.features.publisher.media_manager.service as media_service

    instance = object.__new__(publisher.TweetPublisher)
    instance.browser_manager = MagicMock()
    instance.account_config = AccountConfig(account_id='test')
    instance.llm_service = MagicMock()
    instance.media_dir = str(tmp_path)
    monkeypatch.setattr(publisher, 'generate_post_text_if_needed', AsyncMock(return_value='post'))
    monkeypatch.setattr(media_service, 'download_media', AsyncMock(return_value=None))
    compose = MagicMock(return_value=True)
    monkeypatch.setattr(publisher, 'compose_and_post', compose)
    content = TweetContent(
        text='post', local_media_paths=[str(tmp_path / 'missing.png')],
        media_urls=['https://example.com/failed.png'],
    )
    assert await instance.post_new_tweet(content) is False
    compose.assert_not_called()
