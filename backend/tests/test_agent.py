import os, sys
from pathlib import Path
import pytest
from fastapi import HTTPException
sys.path.insert(0, str(Path(__file__).parents[1]))
from app.schemas.models import Session, DocumentQuery, ProposalQuery
from app.data.repository import Repository
from app.tools.documents import DocumentTool
from app.tools.actions import ActionTool
from app.reliability.rules import evaluate, rank_sources
from app.main import LAST_CONTEXT, local_answer

ROOT=Path(__file__).parents[2]
def make_runtime(tmp_path):
    from app.data.ingest import load_workbook, build_documents
    db=tmp_path/'db.sqlite'; chroma=tmp_path/'chroma'; wb=load_workbook(ROOT/'data/raw',db); docs=build_documents(ROOT/'data/raw',chroma)
    return Repository(db,wb['dataset_now']), docs

def make_agent_runtime(tmp_path):
    """Small in-process runtime for realistic natural-language agent tests."""
    from types import SimpleNamespace
    repo, docs=make_runtime(tmp_path)
    return SimpleNamespace(
        repo=repo, docs=docs, dataset_now=repo.dataset_now,
        documents=DocumentTool(docs,tmp_path/'agent_chroma'),
        actions=ActionTool(tmp_path/'agent_actions.sqlite'),
    )

def tool_names(result):
    return [event['name'] for event in result['events'] if event['type']=='tool']

def pending_proposal(result):
    return next(event['result'] for event in result['events'] if event['name']=='propose_escalation')

def test_unauthorized_account_blocked_across_structured_and_documents(tmp_path):
    repo, docs=make_runtime(tmp_path); user=Session(user_id='arjun',role='support_agent',allowed_account_ids=['ACCT-002'])
    with pytest.raises(HTTPException): repo.order('ORD-1001',user)
    with pytest.raises(HTTPException): repo.account('ACCT-001',user)
    result=DocumentTool(docs,tmp_path/'c').search(DocumentQuery(query='Northstar cancellation',account_id='ACCT-001'),user)
    assert not any(x.get('account_id')=='ACCT-001' for x in result['results'])

def test_confirmation_required(tmp_path):
    tool=ActionTool(tmp_path/'actions.sqlite'); user=Session(user_id='manager',role='ops_manager',all_accounts=True)
    proposal=tool.propose(ProposalQuery(account_id='ACCT-001',reason='P1 verification',severity='P1'),user)
    assert proposal['status']=='pending_confirmation'
    with pytest.raises(HTTPException): tool.confirm(proposal['proposal_id'],False,user)
    executed=tool.confirm(proposal['proposal_id'],True,user); assert executed['status']=='created'
    again=tool.confirm(proposal['proposal_id'],True,user); assert again['action_id']==executed['action_id']

def test_source_precedence_and_deprecation(tmp_path):
    _, docs=make_runtime(tmp_path); current=[{'citation_id':d['id'],**d['metadata']} for d in docs]
    ranked=rank_sources(current,'ACCT-001'); assert all(x['status']!='deprecated' for x in ranked)
    assert ranked[0]['source_type']=='agreement'
    assert any(x['file_name'].startswith('03_') for x in ranked)

def test_agreement_overrides_default_cancellation(tmp_path):
    repo, docs=make_runtime(tmp_path); manager=Session(user_id='manager',role='ops_manager',all_accounts=True)
    lookup=repo.order('ORD-1001',manager); result=evaluate(lookup['record']['fields'],lookup['related']['account'],[{'citation_id':d['id'],'text':d['text'],**d['metadata']} for d in docs],'cancellation',None,lookup['dataset_now'])
    assert result['fee_inr']==0 and result['result']=='eligible'

def test_agreement_without_waiver_keeps_default_cancellation_fee(tmp_path):
    repo, docs=make_runtime(tmp_path); manager=Session(user_id='manager',role='ops_manager',all_accounts=True)
    lookup=repo.order('ORD-2001',manager); result=evaluate(lookup['record']['fields'],lookup['related']['account'],[{'citation_id':d['id'],'text':d['text'],**d['metadata']} for d in docs],'cancellation',None,lookup['dataset_now'])
    assert result['fee_inr']==250 and result['recommended_next_step']=='Cancel with the applicable INR 250 fee'

def test_ticket_history_is_unverified(tmp_path):
    _, docs=make_runtime(tmp_path); ticket=next(x for x in docs if x['metadata']['source_type']=='policy')
    marked=rank_sources([{'citation_id':'ticket','source_type':'ticket_history','status':'context_only','account_id':'ACCT-001','authority_rank':0,'file_name':'history','text':'old'}],'ACCT-001')
    assert marked==[]

def test_generalizes_to_other_record(tmp_path):
    repo,_=make_runtime(tmp_path); manager=Session(user_id='manager',role='ops_manager',all_accounts=True)
    assert repo.order('ORD-2001',manager)['record']['fields']['account_id']=='ACCT-002'
    assert repo.order('ORD-3001',manager)['record']['fields']['account_id']=='ACCT-003'

def test_consecutive_messages_are_fresh_and_route_to_their_own_tools(tmp_path):
    """Regression for the reported lookup-response replay during escalation."""
    LAST_CONTEXT.clear(); r=make_agent_runtime(tmp_path)
    priya=Session(user_id='priya',role='support_agent',allowed_account_ids=['ACCT-001'])
    lookup=local_answer('What is the order status and account name for order ORD-1002?',priya,r)
    escalation=local_answer('Please create an escalation for order ORD-1002 right now.',priya,r)
    assert lookup['answer'] != escalation['answer']
    assert tool_names(lookup) == ['lookup_records']
    assert tool_names(escalation) == ['lookup_records','propose_escalation']
    proposal=pending_proposal(escalation); assert proposal['status']=='pending_confirmation'
    assert r.actions.confirm(proposal['proposal_id'],True,priya)['status']=='created'

@pytest.mark.parametrize('phrase,needs_context',[
    ('create an escalation for order ORD-1002',False),
    ('please escalate this',True),
    ('open a follow-up ticket for this order',True),
])
def test_natural_language_action_phrases_propose_then_confirm(tmp_path,phrase,needs_context):
    LAST_CONTEXT.clear(); r=make_agent_runtime(tmp_path)
    priya=Session(user_id='priya',role='support_agent',allowed_account_ids=['ACCT-001'])
    if needs_context:
        local_answer('Show me the details for ORD-1002',priya,r)
    result=local_answer(phrase,priya,r)
    assert tool_names(result) == ['lookup_records','propose_escalation']
    proposal=pending_proposal(result); assert proposal['status']=='pending_confirmation'
    executed=r.actions.confirm(proposal['proposal_id'],True,priya)
    assert executed['status']=='created' and executed['action']['order_id']=='ORD-1002'

def test_order_specific_sla_credit_uses_lookup_and_calculation(tmp_path):
    LAST_CONTEXT.clear(); r=make_agent_runtime(tmp_path)
    priya=Session(user_id='priya',role='support_agent',allowed_account_ids=['ACCT-001'])
    result=local_answer("What's the SLA credit owed on order ORD-1002?",priya,r)
    assert tool_names(result) == ['lookup_records','search_documents','evaluate_entitlement']
    evaluation=next(event['result'] for event in result['events'] if event['name']=='evaluate_entitlement')
    assert evaluation['evaluation_type']=='service_credit'
    assert evaluation['result']=='not_eligible'
    assert 'service-credit' in result['answer']
