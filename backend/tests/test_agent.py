import asyncio, os, sys
from pathlib import Path
import pytest
from fastapi import HTTPException
sys.path.insert(0, str(Path(__file__).parents[1]))
from app.schemas.models import Session, DocumentQuery, ProposalQuery, LookupQuery, AnalyticsQuery
from app.data.repository import Repository
from app.tools.documents import DocumentTool
from app.tools.actions import ActionTool
from app.tools.analytics import AnalyticsTool
from app.reliability.rules import evaluate, rank_sources
from app.main import LAST_CONTEXT, local_answer, run_agent
import app.main as main_module

ROOT=Path(__file__).parents[2]
def make_runtime(tmp_path):
    from app.data.ingest import load_workbook, build_documents
    db=tmp_path/'db.sqlite'; chroma=tmp_path/'chroma'; wb=load_workbook(ROOT/'data/raw',db); docs=build_documents(ROOT/'data/raw',chroma)
    return Repository(db,wb['dataset_now']), docs

def make_agent_runtime(tmp_path):
    """Small in-process runtime for realistic natural-language agent tests."""
    from types import SimpleNamespace
    repo, docs=make_runtime(tmp_path)
    runtime=SimpleNamespace(
        repo=repo, docs=docs, dataset_now=repo.dataset_now,
        documents=DocumentTool(docs,tmp_path/'chroma'),
        actions=ActionTool(tmp_path/'agent_actions.sqlite'),
    )
    runtime.analytics=AnalyticsTool(repo)
    return runtime

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
    assert tool_names(escalation) == ['propose_escalation']
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
    assert tool_names(result) == ['propose_escalation']
    proposal=pending_proposal(result); assert proposal['status']=='pending_confirmation'
    executed=r.actions.confirm(proposal['proposal_id'],True,priya)
    assert executed['status']=='created' and executed['action']['order_id']=='ORD-1002'

def test_order_specific_sla_credit_uses_lookup_and_calculation(tmp_path):
    LAST_CONTEXT.clear(); r=make_agent_runtime(tmp_path)
    priya=Session(user_id='priya',role='support_agent',allowed_account_ids=['ACCT-001'])
    result=local_answer("What's the SLA credit owed on order ORD-1002?",priya,r)
    assert tool_names(result) == ['evaluate_entitlement']
    evaluation=next(event['result'] for event in result['events'] if event['name']=='evaluate_entitlement')
    assert evaluation['evaluation_type']=='service_credit'
    assert evaluation['result']=='not_eligible'
    assert 'service-credit' in result['answer']

def test_cross_account_listing_is_denied_by_repository(tmp_path):
    repo,_=make_runtime(tmp_path)
    arjun=Session(user_id='arjun',role='support_agent',allowed_account_ids=['ACCT-002'])
    with pytest.raises(HTTPException,match='Cross-account record access'):
        repo.lookup(LookupQuery(record_type='account',record_id=None,query_scope='other_accounts'),arjun)

def test_real_ticket_analytics_finds_no_cross_customer_recurrence(tmp_path):
    repo,_=make_runtime(tmp_path); tool=AnalyticsTool(repo)
    manager=Session(user_id='manager',role='ops_manager',all_accounts=True)
    result=tool.analyze(AnalyticsQuery(),manager)
    assert result['source']=='SQLite tickets table'
    assert result['ticket_count']==7 and len(result['accounts_analyzed'])==4
    assert result['no_significant_recurring_issues'] is True
    repeated_ids={ticket for group in result['same_customer_repeats'] for ticket in group['ticket_ids']}
    assert {'TKT-451','TKT-502'} <= repeated_ids

def test_viewer_action_denial_is_explicit_not_document_fallback(tmp_path):
    LAST_CONTEXT.clear(); r=make_agent_runtime(tmp_path)
    viewer=Session(user_id='viewer',role='viewer',allowed_account_ids=['ACCT-003'])
    result=local_answer('Start a follow-up for ORD-3001.',viewer,r)
    assert tool_names(result)==['propose_escalation']
    assert result['events'][0]['status']=='denied'
    assert 'not permitted' in result['answer'] and 'policy' not in result['answer'].lower()

class NativeToolProvider:
    def __init__(self,name,args): self.name,self.args=name,args; self.seen=None
    async def complete(self,messages,tools,tool_choice='auto'):
        self.seen=(messages,tools,tool_choice)
        return {'_provider':'test','tool_calls':[{'function':{'name':self.name,'arguments':self.args}}]}

def test_native_model_tool_call_is_authoritative_and_receives_all_tools(tmp_path):
    LAST_CONTEXT.clear(); r=make_agent_runtime(tmp_path)
    manager=Session(user_id='manager',role='ops_manager',all_accounts=True)
    fake=NativeToolProvider('analyze_operations',{'analysis_type':'recurring_ticket_issues','scope':'all_accounts','min_accounts':2,'include_closed':True})
    result=asyncio.run(run_agent('Give me the operational picture.',manager,r,router_provider=fake))
    assert tool_names(result)==['analyze_operations'] and result['model_routing']=='native:test'
    assert fake.seen[2]=='required'
    assert {x['function']['name'] for x in fake.seen[1]}=={'search_documents','lookup_records','evaluate_entitlement','analyze_operations','propose_escalation'}

def test_unsafe_native_misroute_is_corrected_before_viewer_role_denial(tmp_path):
    LAST_CONTEXT.clear(); r=make_agent_runtime(tmp_path)
    viewer=Session(user_id='viewer',role='viewer',allowed_account_ids=['ACCT-003'])
    fake=NativeToolProvider('search_documents',{'query':'escalation'})
    result=asyncio.run(run_agent('Raise an escalation for ORD-3001.',viewer,r,router_provider=fake))
    assert tool_names(result)==['propose_escalation']
    assert result['events'][0]['status']=='denied'
    assert result['model_routing']=='native:test+safety_correction'

def test_native_document_misroutes_cannot_hide_cross_scope_or_analytics(tmp_path):
    LAST_CONTEXT.clear(); r=make_agent_runtime(tmp_path)
    arjun=Session(user_id='arjun',role='support_agent',allowed_account_ids=['ACCT-002'])
    manager=Session(user_id='manager',role='ops_manager',all_accounts=True)
    wrong=NativeToolProvider('search_documents',{'query':'generic policy'})
    cross=asyncio.run(run_agent("Retrieve another agent's customer portfolio.",arjun,r,router_provider=wrong))
    aggregate=asyncio.run(run_agent('Find recurring complaints across customer accounts.',manager,r,router_provider=wrong))
    assert tool_names(cross)==['lookup_records'] and cross['events'][0]['status']=='denied'
    assert tool_names(aggregate)==['analyze_operations'] and aggregate['events'][0]['status']=='complete'
    assert cross['model_routing'].endswith('+safety_correction') and aggregate['model_routing'].endswith('+safety_correction')

@pytest.mark.parametrize('user,message,expected_tool,expected_status,expected_text',[
    ('priya','List the shipment records currently assigned to me.','lookup_records','complete','Found 2 authorized order records'),
    ('priya','What does the current support policy call a critical incident?','search_documents','complete','authoritative passages'),
    ('priya','Would cancelling ORD-1001 incur any charge?','evaluate_entitlement','complete','Cancellation fee: INR 0'),
    ('arjun','Bring up the facts recorded for TKT-502.','lookup_records','complete','Authorized details for TKT-502'),
    ('arjun',"Pull the order history belonging to another agent's client.",'lookup_records','denied','Access denied'),
    ('arjun','How much service credit is due for ORD-2002?','evaluate_entitlement','complete','service-credit'),
    ('manager','Which issue themes repeat among several customer accounts?','analyze_operations','complete','No significant recurring issue'),
    ('manager','Review complaint trends across multiple customers.','analyze_operations','complete','real tickets'),
    ('manager','List every customer account in the supplied snapshot.','lookup_records','complete','Found 4 authorized account records'),
    ('viewer','What complaints are recorded for my customer?','lookup_records','complete','authorized ticket records'),
    ('viewer','Start a case escalation for ORD-3001.','propose_escalation','denied','not permitted'),
    ('priya','Open a followup on the shipment we just reviewed.','propose_escalation','complete','pending your explicit confirmation'),
])
def test_natural_language_routing_matrix(tmp_path,user,message,expected_tool,expected_status,expected_text):
    LAST_CONTEXT.clear(); r=make_agent_runtime(tmp_path)
    sessions={
        'priya':Session(user_id='priya',role='support_agent',allowed_account_ids=['ACCT-001']),
        'arjun':Session(user_id='arjun',role='support_agent',allowed_account_ids=['ACCT-002']),
        'manager':Session(user_id='manager',role='ops_manager',all_accounts=True),
        'viewer':Session(user_id='viewer',role='viewer',allowed_account_ids=['ACCT-003']),
    }
    session=sessions[user]
    if message=='Open a followup on the shipment we just reviewed.':
        local_answer('Retrieve ORD-1002 for review.',session,r)
    result=local_answer(message,session,r)
    assert tool_names(result)==[expected_tool]
    assert result['events'][0]['status']==expected_status
    assert expected_text.lower() in result['answer'].lower()


@pytest.mark.parametrize('query,expected_section,expected_text',[
    ('What are the P1 severity rules?','2. Severity definitions','Complete production outage'),
    ('What are the Enterprise first-response targets?','3. Default first-response targets','30 minutes'),
    ('What are the BOOKED cancellation fees after 30 minutes?','1. Order cancellation','INR 250'),
    ('Explain KI-208 bulk upload failures.','KI-208 - Bulk Upload failures','below 3,000 rows'),
    ('Explain KI-211 SwiftShip webhook delay.','KI-211 - SwiftShip pickup webhook delay','up to 20 minutes late'),
    ('What are the Growth plan bulk upload limits?','1. Plan capabilities','up to 5,000 rows'),
])
def test_document_retrieval_returns_topically_relevant_sections(tmp_path,query,expected_section,expected_text):
    _,docs=make_runtime(tmp_path); manager=Session(user_id='manager',role='ops_manager',all_accounts=True)
    result=DocumentTool(docs,tmp_path/'chroma').search(DocumentQuery(query=query),manager)
    assert result['results']
    top=result['results'][0]
    assert expected_section in top['section']
    assert expected_text.lower() in top['text'].lower()
    assert 'Scope and source precedence' not in top['section']


def test_lightweight_embeddings_are_stable_and_topic_discriminating():
    from app.data.embeddings import embed_text
    def similarity(left,right): return sum(a*b for a,b in zip(embed_text(left),embed_text(right)))
    cancellation='BOOKED shipment cancellation fee waiver before pickup'
    assert embed_text(cancellation)==embed_text(cancellation)
    assert similarity(cancellation,'cancel a booked shipment with no fee') > similarity(cancellation,'P1 security incident response target')


@pytest.mark.parametrize('query,expected_text',[
    ('What is known about KI-208 bulk upload failures?','below 3,000 rows'),
    ('Do we have details for known issue KI-211?','up to 20 minutes late'),
    ('What is the Growth plan CSV row limit?','up to 5,000 rows'),
    ('How does the current policy define P1 severity?','Complete production outage'),
    ('What are the Enterprise response targets?','30 minutes'),
])
def test_explicit_document_questions_cannot_be_misrouted_by_model(tmp_path,query,expected_text):
    LAST_CONTEXT.clear(); r=make_agent_runtime(tmp_path)
    manager=Session(user_id='manager',role='ops_manager',all_accounts=True)
    class WrongRouter:
        async def complete(self,*args,**kwargs):
            return {'tool_calls':[{'function':{'name':'analyze_operations','arguments':{'analysis_type':'recurring_ticket_issues','scope':'all_accounts'}}}], '_provider':'wrong'}
    # A supplied router simulates a hosted-model misclassification. The safety
    # correction must preserve the explicit document intent.
    result=asyncio.run(run_agent(query,manager,r,router_provider=WrongRouter()))
    assert tool_names(result)==['search_documents']
    assert 'safety_correction' in result['model_routing']
    assert expected_text.lower() in result['answer'].lower()


def test_account_named_cancellation_retrieves_contract_and_sop_sections(tmp_path):
    _,docs=make_runtime(tmp_path); priya=Session(user_id='priya',role='support_agent',allowed_account_ids=['ACCT-001'])
    result=DocumentTool(docs,tmp_path/'chroma').search(DocumentQuery(query="Can Northstar cancel a BOOKED shipment before pickup without a fee?"),priya)
    top=result['results'][:3]; sections=[x['section'] for x in top]
    assert result['resolved_account_id']=='ACCT-001'
    assert any(x['source_type']=='agreement' and x['account_id']=='ACCT-001' and 'Shipment cancellation' in x['section'] for x in top)
    assert any(x['source_type']=='sop' and 'Order cancellation' in x['section'] for x in top)


def test_exact_customer_named_scenarios_are_complete_without_model(tmp_path):
    LAST_CONTEXT.clear(); r=make_agent_runtime(tmp_path)
    priya=Session(user_id='priya',role='support_agent',allowed_account_ids=['ACCT-001'])
    arjun=Session(user_id='arjun',role='support_agent',allowed_account_ids=['ACCT-002'])
    northstar=asyncio.run(run_agent("Can Northstar cancel a shipment booked 2 hours ago that hasn't been picked up? Any fee?",priya,r))
    lumen=asyncio.run(run_agent('LumenWorks had a pickup that was 5 hours late, carrier at fault, no customer fault. What credit are they owed?',arjun,r))
    assert tool_names(northstar)==['evaluate_entitlement'] and northstar['model_routing']=='deterministic_intent'
    assert 'ORD-1001' in northstar['answer'] and 'Cancellation fee: INR 0' in northstar['answer'] and 'full waiver' in northstar['answer']
    assert '2. Shipment cancellation' in northstar['answer']
    assert tool_names(lumen)==['evaluate_entitlement'] and lumen['model_routing']=='deterministic_intent'
    assert 'ORD-2002' in lumen['answer'] and 'Credit owed: INR 300' in lumen['answer']
    assert 'carrier fault yes' in lumen['answer'] and 'customer fault no' in lumen['answer'] and '3. Failed-pickup credits' in lumen['answer']


def test_customer_named_entitlement_cannot_cross_account_scope(tmp_path):
    LAST_CONTEXT.clear(); r=make_agent_runtime(tmp_path)
    arjun=Session(user_id='arjun',role='support_agent',allowed_account_ids=['ACCT-002'])
    result=local_answer("Can Northstar cancel a shipment booked 2 hours ago without a fee?",arjun,r)
    assert tool_names(result)==['evaluate_entitlement']
    assert result['events'][0]['status']=='denied'
    assert 'outside this session scope' in result['answer']


def test_daily_model_limit_never_blocks_deterministic_document_answer(tmp_path,monkeypatch):
    LAST_CONTEXT.clear(); r=make_agent_runtime(tmp_path)
    manager=Session(user_id='manager',role='ops_manager',all_accounts=True)
    class MustNotRun:
        async def complete(self,*args,**kwargs): raise AssertionError('provider must not run after daily limit')
    monkeypatch.setattr(main_module,'GROQ_API_KEY','configured')
    monkeypatch.setattr(main_module,'provider',MustNotRun())
    monkeypatch.setattr(main_module,'model_call_day',main_module.date.today())
    monkeypatch.setattr(main_module,'model_calls_today',main_module.MODEL_DAILY_LIMIT)
    result=asyncio.run(run_agent('What does the current support policy say about P1 severity?',manager,r))
    assert result['model_routing']=='deterministic_intent'
    assert tool_names(result)==['search_documents']
    assert 'Complete production outage' in result['answer']
