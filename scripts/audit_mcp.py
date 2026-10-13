"""Exercise every registered tool over stdio in a credential-free temporary home.

Runs schema/error checks for every tool plus real local workflows, all prompts,
all resources, and restart persistence. Browser/LLM successes belong to the
separate synthetic browser and unit suites; this never authenticates or sends.
Use an installed wheel's Python to audit its actual module entry point.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import sys
import tempfile
import time

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def sample(schema):
    if schema.get('enum'):
        return schema['enum'][0]
    for option in schema.get('anyOf', []):
        if option.get('type') != 'null':
            return sample(option)
    kind = schema.get('type')
    if kind == 'array':
        return [sample(schema.get('items', {}))]
    if kind in ('number', 'integer'):
        return 1
    if kind == 'boolean':
        return False
    if kind == 'object':
        return {}
    return 'audit_missing'


@asynccontextmanager
async def connect(directory, python):
    environment = {**os.environ, 'X_USE_HOME':str(directory), 'PYTHONUTF8':'1'}
    environment.pop('PYTHONPATH', None)
    parameters = StdioServerParameters(command=python, args=['-m','xuse.mcp.server'],
                                       cwd=str(directory), env=environment)
    with (directory/'stderr.log').open('a',encoding='utf-8') as errors:
        async with stdio_client(parameters,errlog=errors) as (read,write):
            async with ClientSession(read,write) as session:
                await session.initialize()
                yield session


async def audit(directory, python):
    from xuse.core.local_state import private_state_file
    from xuse.setup_defaults import DEFAULT_SETTINGS

    for name, value in [('settings.json',DEFAULT_SETTINGS),('accounts.json',[])]:
        path=directory/'config'/name
        private_state_file(path)
        path.write_text(json.dumps(value),encoding='utf-8')
    report={'tool_calls':[], 'external_writes':0, 'live_authentication':False}

    async def call(session,name,args=None,expected=True,phase='local_workflow'):
        start=time.monotonic()
        result=await session.call_tool(name,args or {})
        content=json.loads(result.content[0].text)
        structured=result.structured_content
        assert structured is not None, f'{name}: no structured result'
        assert structured.get('result',structured)==content, f'{name}: result representations disagree'
        assert result.is_error is (not content['ok']), f'{name}: isError disagrees with envelope'
        if expected is not None:
            assert content['ok'] is expected, f'{name}: {content}'
        report['tool_calls'].append({'tool':name,'phase':phase,'ok':content['ok'],
                                    'seconds':round(time.monotonic()-start,4)})
        return content

    async with connect(directory,python) as session:
        tools=(await session.list_tools()).tools
        names={tool.name for tool in tools}
        report['tools']=sorted(names)
        for tool in tools:
            assert tool.annotations is not None
            assert tool.input_schema['type']=='object'
            args={key:sample(tool.input_schema['properties'][key])
                  for key in tool.input_schema.get('required',[])}
            # No account exists, and these controls prevent configuration or
            # network mutations even for account-independent optional tools.
            if tool.name=='add_account':
                args={'account_id':'../invalid'}
            elif tool.name=='add_proxy':
                args={'pool':'invalid','proxy_url':'not-a-url'}
            elif tool.name=='run_cycle':
                args={'account':'audit_missing'}
            elif tool.name=='get_metrics':
                args={'account':'../invalid'}
            elif tool.name=='test_proxy':
                args={'proxy_url':'not-a-url'}
            await call(session,tool.name,args,expected=None,phase='empty_home_contract')
        await call(session,'add_account',{'account_id':'audit','is_active':True,'persona':'Synthetic audit persona'})
        await call(session,'update_account',{'account':'audit','self_handles':['synthetic'],'target_keywords':['audit']})
        await call(session,'set_account_active',{'account':'audit','active':False})
        await call(session,'set_account_active',{'account':'audit','active':True})
        for name in ('get_account','get_account_health','get_metrics','get_account_safety'):
            await call(session,name,{'account':'audit'})
        await call(session,'pause_account_actions',{'account':'audit'})
        # Resume would start an authenticated browser; tested offline elsewhere.
        await call(session,'add_proxy',{'pool':'audit','proxy_url':'http://127.0.0.1:9'})
        await call(session,'list_proxies')
        await call(session,'remove_proxy',{'pool':'audit','proxy_url':'http://127.0.0.1:9','confirm':True})
        for name,args in [
            ('post_tweet',{'text':'Synthetic draft only'}),
            ('reply_to_tweet',{'tweet_url':'https://x.com/synthetic/status/123','text':'Synthetic reply only'}),
            ('send_message',{'recipient':'@synthetic','text':'Synthetic DM only'}),
            ('follow_profile',{'profile':'synthetic'}),
            ('bookmark_tweet',{'tweet_url':'https://x.com/synthetic/status/123'}),
            ('remove_bookmark',{'tweet_url':'https://x.com/synthetic/status/123'}),
            ('unlike_tweet',{'tweet_url':'https://x.com/synthetic/status/123'}),
            ('undo_retweet',{'tweet_url':'https://x.com/synthetic/status/123'}),
            ('unfollow_profile',{'profile':'synthetic'}),
            ('edit_tweet',{'tweet_url':'https://x.com/synthetic/status/123','expected_text':'Before','text':'After'}),
            ('delete_tweet',{'tweet_url':'https://x.com/synthetic/status/123','expected_text':'Before'}),
            ('manage_conversation',{'conversation_id':'https://x.com/messages/10-20','operation':'mute'}),
        ]:
            draft=await call(session,name,{'account':'audit',**args})
            await call(session,'get_draft',{'draft_id':draft['draft_id']})
            await call(session,'reject_draft',{'draft_id':draft['draft_id']})
            await call(session,'approve_draft',{'draft_id':draft['draft_id']},expected=False)
        thread=await call(session,'prepare_thread',{'account':'audit','posts':[{'text':'Part one'},{'text':'Part two'}]})
        run_id=thread.get('run_id') or thread.get('payload',{}).get('run_id')
        await call(session,'get_thread_run',{'run_id':run_id})
        await call(session,'cancel_thread',{'run_id':run_id})
        cancelled=await call(session,'continue_thread',{'run_id':run_id})
        assert cancelled['state']=='cancelled' and not cancelled['published']
        post=await call(session,'queue_post',{'account':'audit','text':'Never send this queue fixture'})
        engagement=await call(session,'queue_engagement',{'account':'audit','action':'like','tweet_url':'https://x.com/synthetic/status/123'})
        for item in (post,engagement):
            await call(session,'cancel_queued_action',{'queue_id':item['queue_id']})
        await call(session,'list_queue',{'account':'audit'})
        await call(session,'process_queue',{'account':'audit'})
        lead=(await call(session,'upsert_lead',{'account':'audit','handle':'synthetic','display_name':'Synthetic'}))['lead']
        lid=lead['lead_id']
        for name,args in [('get_lead',{'lead_id':lid}),('list_leads',{}),('update_lead_status',{'lead_id':lid,'status':'qualified'})]:
            await call(session,name,{'account':'audit',**args})
        campaign=(await call(session,'create_campaign',{'account':'audit','name':'Synthetic','message_template':'Hi {name}'}))['campaign']
        cid=campaign['campaign_id']
        await call(session,'add_campaign_leads',{'account':'audit','campaign_id':cid,'lead_ids':[lid]})
        if 'preview_campaign_message' in names:
            await call(session,'preview_campaign_message',{'account':'audit','campaign_id':cid,'lead_id':lid})
        prepared=await call(session,'prepare_campaign_messages',{'account':'audit','campaign_id':cid})
        for draft in prepared['drafts']:
            await call(session,'reject_draft',{'draft_id':draft['draft_id']})
        await call(session,'opt_out_lead',{'account':'audit','lead_id':lid})
        for name,args in [('get_campaign',{}),('get_campaign_summary',{}),('set_campaign_status',{'status':'paused'})]:
            await call(session,name,{'account':'audit','campaign_id':cid,**args})
        await call(session,'list_campaigns',{'account':'audit'})
        prompts=(await session.list_prompts()).prompts
        for prompt in prompts:
            args={arg.name:'audit' if arg.name=='account' else 'synthetic' for arg in prompt.arguments or []}
            result=await session.get_prompt(prompt.name,args)
            assert result.messages
        report['prompts']=sorted(p.name for p in prompts)
        resources=(await session.list_resources()).resources
        templates=(await session.list_resource_templates()).resource_templates
        uris=[str(r.uri) for r in resources]+[str(r.uri_template).replace('{account_id}','audit') for r in templates]
        for uri in uris:
            result=await session.read_resource(uri)
            assert result.contents
        report['resources']=uris
        await call(session,'close_session',{'account':'audit'})
        state=await call(session,'get_session_status')
        report['session_status']=state
    async with connect(directory,python) as session:
        result=await call(session,'list_drafts',{'status':'pending'},phase='restart')
        assert result['total']==0
        await call(session,'get_lead',{'account':'audit','lead_id':lid},phase='restart')
        result=await call(session,'get_campaign_summary',{'account':'audit','campaign_id':cid},phase='restart')
        await call(session,'remove_account',{'account':'audit','confirm':True},phase='restart')
    report['tool_count']=len(names)
    report['distinct_tools_called']=len({r['tool'] for r in report['tool_calls']})
    report['success']=True
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--python',default=sys.executable)
    parser.add_argument('--report',type=Path,required=True)
    args=parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='xuse-protocol-audit-') as temporary:
        # macOS may return a /var path whose parent is a system symlink.
        # Canonicalize only this directory we created; production state paths
        # still reject links rather than silently resolving operator inputs.
        directory=Path(temporary).resolve()
        report=asyncio.run(audit(directory,args.python))
    args.report.parent.mkdir(parents=True,exist_ok=True)
    args.report.write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps({key:report[key] for key in ('success','tool_count','distinct_tools_called','external_writes')}))


if __name__=='__main__':
    main()
