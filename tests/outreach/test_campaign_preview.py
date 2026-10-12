"""Preview purity, suppression and cross-campaign rejection recovery."""
import pytest

from xuse.mcp.drafts import DraftStore
from xuse.outreach import OutreachStore, OutreachError


@pytest.fixture
def records(tmp_path):
    store = OutreachStore(tmp_path / 'outreach.sqlite3')
    drafts = DraftStore(tmp_path / 'drafts.jsonl')
    lead = store.upsert_lead('a', 'Ada', display_name='Ada', notes='PRIVATE-NOTES')
    campaigns = [store.create_campaign('a', str(i), 'Hello {name}') for i in range(2)]
    for campaign in campaigns:
        store.add_campaign_leads('a', campaign.campaign_id, [lead.lead_id])
    return store, drafts, lead, campaigns


def test_preview_does_not_reserve_or_change_status(records):
    store, drafts, lead, campaigns = records
    before = store.get_campaign('a', campaigns[0].campaign_id).model_dump()
    preview = store.preview_campaign_message('a', campaigns[0].campaign_id, lead.lead_id, drafts)
    assert preview['text'] == 'Hello Ada'
    assert preview['eligible'] is True
    assert 'PRIVATE-NOTES' not in str(preview)
    assert len(drafts) == 0
    assert store.get_campaign('a', campaigns[0].campaign_id).model_dump() == before


@pytest.mark.parametrize('status,eligible', [('pending',False),('rejected',True),
    ('failed',False),('uncertain',False),('approved',False),('executed',False)])
def test_cross_campaign_reservation_uses_current_draft_outcome(records, status, eligible):
    store, drafts, lead, campaigns = records
    first = store.prepare_campaign_messages('a', campaigns[0].campaign_id, drafts)[0]
    drafts.set_status(first.draft_id, status)
    preview = store.preview_campaign_message('a', campaigns[1].campaign_id, lead.lead_id, drafts)
    assert preview['eligible'] is eligible
    prepared = store.prepare_campaign_messages('a', campaigns[1].campaign_id, drafts)
    assert bool(prepared) is eligible


def test_missing_draft_remains_reserved(records):
    store, drafts, lead, campaigns = records
    store.prepare_campaign_messages('a', campaigns[0].campaign_id, drafts)
    assert not store.preview_campaign_message('a', campaigns[1].campaign_id,
                                               lead.lead_id, DraftStore())['eligible']


def test_preview_reports_pause_and_permanent_opt_out(records):
    store, drafts, lead, campaigns = records
    store.opt_out_lead('a', lead.lead_id)
    store.set_campaign_status('a', campaigns[0].campaign_id, 'paused')
    preview = store.preview_campaign_message('a', campaigns[0].campaign_id, lead.lead_id, drafts)
    assert preview['eligible'] is False
    assert set(preview['reasons']) == {'opted_out', 'campaign_paused'}


def test_preview_requires_account_and_membership(records):
    store, drafts, lead, campaigns = records
    with pytest.raises(OutreachError, match='Unknown campaign'):
        store.preview_campaign_message('b', campaigns[0].campaign_id, lead.lead_id, drafts)
    outsider = store.upsert_lead('a', 'Outsider')
    with pytest.raises(OutreachError, match='not a member'):
        store.preview_campaign_message('a', campaigns[0].campaign_id, outsider.lead_id, drafts)
