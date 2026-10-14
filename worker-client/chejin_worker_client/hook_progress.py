"""Presentation-only native progress, published through the original UI signal."""
from datetime import datetime,timezone
import threading
import re

PHASES = {
 'environment': '检查运行环境', 'ready': '运行环境检查通过',
 'wechat_window': '调整微信窗口位置和大小',
 'worker_window': '将 Worker 放到微信旁边',
 'invite_received': '领取加好友任务', 'invite_prepare': '准备申请文案和备注',
 'search_empty': '定位微信搜索框', 'query_verified_and_network_lookup': '输入手机号并查找客户',
 'contact_result': '确认搜索结果', 'invite_form_fields': '填写申请文案和备注',
 'verify_remark_shortcode': '核对备注短码', 'confirm_once': '提交好友申请',
 'cleanup': '清理本次搜索内容', 'invite_receipt': '确认邀请任务结果',
 'bind': '等待好友通过并绑定客户', 'history': '读取并补齐客户消息',
 'voice': '语音转文字', 'decision': 'AI 处理客户消息', 'reply_ready': 'AI 回复已准备',
 'send_conditions': '核验发送条件', 'send_delay': '等待发送间隔',
 'pre_send': '发送前复核最新消息', 'send_submit': '向微信提交回复',
 'send_confirm': '确认发送结果', 'finished': '本次流程已结束',
 'messages_saved': '接收并保存客户消息', 'waiting': '等待下一条客户消息', 'connection_wait': '等待连接恢复',
 'sync_finished': '本次消息核对已结束',
 'handoff': '已转人工，等待销售处理',
 'handoff_closed': '人工交接已结束',
 'clock_wait': '等待后台响应稳定', 'stopped': '已停止接单',
 'model_queue': '等待 AI 处理', 'model_retry': '等待 AI 重试',
 'invite_finished': '好友申请处理已结束',
}
NOTES = {
 'recipient': '申请已提交，等待对方通过。', 'not_found': '未找到可添加的客户。',
 'already_friend': '对方已是好友，沿原任务规则处理。',
 'history_wait': '消息尚未核对完整，核对完成后继续。',
 'voice_wait': '语音结果尚未确认，保留原记录。',
 'model_wait': '等待后台处理结果。', 'no_reply': '本次消息无需发送回复。',
 'handoff': '此客户的 AI 自动回复已暂停，等待销售处理。',
 'handoff_ai_failure': 'AI 回复处理失败，已按规则转人工。',
 'handoff_voice_unknown': '语音转写结果未确认，已转人工处理；原语音不会自动重试。',
 'handoff_closed': '这次人工交接已结束。', 'send_wait': '发送结果待核对，不重复发送。',
 'connection_wait': '后台暂时无法响应，已收到的消息仍保留。',
 'clock_wait': '后台响应较慢，完成时间校验后继续。',
 'paused': '已暂停新任务，已登记的流程按原规则收尾。',
 'failed': '本次处理未完成，请查看故障提示。',
 'incomplete': '流程已结束，这一步尚未确认完成。',
 'send_queue': '回复已准备，等待发送条件通过。',
 'queued': '消息已核对，等待开始 AI 处理。',
 'retry': '本轮暂未生成可用回复，等待后台重试。',
 'callback_wait': '等待对应消息记录到齐，核对完成后继续。',
 'paused_work': '已暂停接单，开始接单后继续处理。',
 'interrupted': '处理已中断，故障恢复后按原记录核对结果。',
 'not_sent': '本轮回复未发送，旧回复已取消。',
 'sent': '本轮回复已发送并核对完成。',
 'invite_done': '好友申请阶段已结束；通过后的客户处理另行展示。',
}


_REGISTRY_LOCK = threading.RLock()


def register_progress(runner, context, *, group=None, label='', kind='customer',
                      historical=False, aliases=(), segment=None):
    """Explicit business identities only. This registry never authorizes work."""
    if runner is None:return
    context=str(context);group=str(group or context)
    safe_label=label if isinstance(label,str) and re.fullmatch(r'[A-Za-z0-9]{1,32}',label) else ''
    with _REGISTRY_LOCK:
        registry=getattr(runner,'_hook_progress_contexts',None)
        if registry is None:registry={};runner._hook_progress_contexts=registry
        prior=registry.get(context)
        # A restart handoff snapshot must not demote a live round to history.
        historical=bool(historical and not (prior and not prior['historical']))
        metadata={'group':group,'label':safe_label or (prior or {}).get('label',''),
                  'kind':kind,'historical':historical}
        if isinstance(segment,int) and 1<=segment<=20:metadata['segment']=segment
        accepted=[]
        for alias in (context,*aliases):
            alias=str(alias);old=registry.get(alias)
            # A new batch can include a parent of an older batch. Keep that
            # completed batch intact; merge only preliminary read/voice rounds.
            if old and old['group']!=group and old.get('kind')=='batch':continue
            registry[alias]=dict(metadata);accepted.append(alias)
        event={'event':'hook_progress_group','context':context,**metadata,'aliases':accepted}
        _deliver(runner,event)


def forget_completed_progress_group(runner, group):
    """Release display aliases only after the UI retired a completed round."""
    if runner is None:
        return
    with _REGISTRY_LOCK:
        registry = getattr(runner, '_hook_progress_contexts', {})
        for context, metadata in list(registry.items()):
            # Invitation discovery may still need its submitted task label.
            if metadata.get('group') == group and metadata.get('kind') in {'batch', 'customer', 'reconcile'}:
                del registry[context]


def _deliver(runner,event):
    try:
        emit=getattr(runner,'_emit_runtime_process',None)
        if emit is not None:emit(event)
        else:
            callback=getattr(runner,'on_runtime_process',None)
            if callback is not None:callback(event)
    except Exception:pass


def emit_progress(runner, context, phase, state='current', note=None, *, occurred_at=None, description=None, affected_group=None):
    if runner is None or phase not in PHASES or state not in {'current','done','waiting','error'}: return
    shown_time=datetime.now().strftime('%H:%M:%S')
    if occurred_at is not None:
        try:
            value=datetime.fromisoformat(str(occurred_at).replace('Z','+00:00'))
            if value.tzinfo is None:value=value.replace(tzinfo=timezone.utc)
            value=value.astimezone()
            shown_time=value.strftime('%H:%M:%S' if value.date()==datetime.now().date() else '%m-%d %H:%M')
        except (TypeError,ValueError,OverflowError):shown_time=''
    event={'event':'hook_progress','context':str(context),'phase':phase,'state':state,
           'time':shown_time,'note':note if note in NOTES else None}
    with _REGISTRY_LOCK:
        event.update(getattr(runner,'_hook_progress_contexts',{}).get(str(context),{}))
    if phase=='stopped' and isinstance(description,str):event['description']=description
    if affected_group is not None:event['affected_group']=str(affected_group)
    _deliver(runner,event)


STARTUP_PHASES = ('environment','wechat_window','worker_window','ready')


class HookProgressTimeline:
    def __init__(self,max_steps=100):
        self.max_steps=max_steps;self.steps=[];self.latest={};self.groups={};self._focus=None;self._retired_groups=set()

    def apply(self,event):
        if event.get('event')=='hook_progress_control':
            if event.get('action')=='fault_clear':
                removed=[s for s in self.steps if s.get('_group')=='system' and s.get('_phase')=='stopped']
                for item in removed:
                    self.steps.remove(item)
                    for key,value in list(self.latest.items()):
                        if value is item:self.latest.pop(key,None)
                    if self._focus is item:self._focus=None
            return
        if event.get('event')=='hook_progress_group':
            self._register(event);return
        phase=event.get('phase');state=event.get('state');context=event.get('context')
        if event.get('event')!='hook_progress' or phase not in PHASES or state not in {'current','done','waiting','error'} or not isinstance(context,str):return
        metadata=self.groups.get(context,{})
        event={**metadata,**event}
        key=(context,phase);old=self.latest.get(key)
        if phase in {'connection_wait','clock_wait'}:
            # One live condition may have both heartbeat and business owners.
            # A successful heartbeat cannot resolve a failed business request.
            if state=='done':
                if old is not None:
                    old['_wait_contexts'].discard(context)
                    self.latest.pop(key,None)
                    if not old['_wait_contexts']:self.steps.remove(old)
                return
            if state in {'current','waiting'}:
                item=next((s for s in self.steps if s.get('_phase')==phase),None)
                if item is None:
                    item={'title':PHASES[phase],'state':state,'_context':context,
                          '_phase':phase,'_wait_contexts':set()}
                    self.steps.append(item)
                item['_wait_contexts'].add(context);self.latest[key]=item
                item.update(_group='system',_kind='system',_historical=False)
                if old is item and item.get('_note')==event.get('note'):return
                item.update(state=state,time=event.get('time',''),_note=event.get('note'),description=NOTES.get(event.get('note'),''))
                self._focus=item
                self._trim()
                return
        if phase=='finished':
            for prior in self.steps:
                if prior.get('_context')==context and prior['state']=='current':
                    prior['state']='error' if event.get('note')=='not_sent' else 'waiting'
                    prior['description']=NOTES['not_sent'] if event.get('note')=='not_sent' else NOTES['incomplete']
        if phase=='stopped':
            # A fault does not invent failures for other customers, and never
            # changes a completed fact. Suspend the identified affected round.
            affected=event.get('affected_group',event.get('group'))
            for prior in self.steps:
                if affected and prior.get('_group')==affected and prior['state']=='current':
                    prior['state']='waiting';prior['description']=NOTES['interrupted']
        # Heartbeat/history polling cannot reopen an already completed stage.
        if old and ((old['state']=='done' and state in {'current','done','waiting'}) or (old['state']==state and old.get('_note')==event.get('note'))):return
        if old and (old['state'] not in {'error','done'} or event.get('group')):
            item=old
        else:
            item={'title':PHASES[phase],'state':'current','_context':context,'_phase':phase};self._insert_step(item);self.latest[key]=item
        item.update(state=state,time=event.get('time',''),_note=event.get('note'))
        item['description']=event.get('description') or NOTES.get(event.get('note'),'')
        item.update(_group=event.get('group',context),_kind=event.get('kind',''),
                    _label=event.get('label',''),_historical=bool(event.get('historical')))
        if phase=='finished':
            if state=='waiting':item['title']='等待本轮处理结果'
            unresolved=any(s is not item and s.get('_group')==item['_group']
                and s['state'] in {'current','waiting','error'} for s in self.steps)
            if event.get('note')=='sent' and unresolved:
                item.update(state='waiting',title='等待本轮处理结果',description=NOTES['send_wait'])
            elif event.get('note')=='sent':item['title']='本轮回复已完成'
            elif event.get('note')=='not_sent':item['title']='本轮回复已取消'
            elif event.get('note')=='no_reply':item['title']='本轮无需回复'
            elif unresolved:item.update(state='waiting',title='本轮结果待确认',description=NOTES['incomplete'])
        if state in {'current','waiting','error'} and not item['_historical']:self._focus=item
        self._trim()

    def _insert_step(self,item):
        # GUI placement and authenticated business polling publish on different
        # threads. Keep startup prerequisites together without delaying reads or
        # changing any business event's status/time/order within its own group.
        if item['_context']=='startup' and item['_phase'] in STARTUP_PHASES:
            rank=STARTUP_PHASES.index(item['_phase'])
            for index,prior in enumerate(self.steps):
                startup=prior.get('_context')=='startup' and prior.get('_phase') in STARTUP_PHASES
                if ((startup and STARTUP_PHASES.index(prior['_phase'])>rank)
                        or (not startup and prior.get('_phase') not in {'connection_wait','clock_wait'})):
                    self.steps.insert(index,item)
                    return
        self.steps.append(item)

    def _trim(self):
        if self.groups:
            # Keep complete rounds and unresolved customer holds. Never leave
            # half a completed waterfall because a new round crossed a limit.
            while len(self.steps)>self.max_steps:
                grouped={}
                for s in self.steps:grouped.setdefault(s.get('_group',s['_context']),[]).append(s)
                def completed(g, items):
                    if g == 'startup' or any(x['state'] in {'current', 'waiting'} for x in items):
                        return False
                    handoff_closed = any(x['_phase'] == 'handoff_closed' and x['state'] == 'done' for x in items)
                    if any(x['_phase'] == 'handoff' for x in items) and not handoff_closed:
                        return False
                    return all(x['state'] == 'done' for x in items) or handoff_closed
                candidate=next((items for g,items in grouped.items() if completed(g,items)),None)
                if not candidate:break
                group = candidate[0].get('_group', candidate[0]['_context'])
                self._retired_groups.add(group)
                for context, metadata in list(self.groups.items()):
                    if metadata.get('group') == group:
                        del self.groups[context]
                for item in candidate:
                    if self._focus is item:self._focus=None
                    self.steps.remove(item)
                    for k,v in list(self.latest.items()):
                        if v is item:del self.latest[k]
            return
        while len(self.steps)>self.max_steps:
            removed=self.steps.pop(0)
            for k,v in list(self.latest.items()):
                if v is removed:del self.latest[k]

    def take_retired_groups(self):
        groups = tuple(self._retired_groups)
        self._retired_groups.clear()
        return groups

    def _register(self,event):
        group=event.get('group');aliases=event.get('aliases',[])
        if not isinstance(group,str) or not isinstance(aliases,list):return
        meta={k:event.get(k) for k in ('group','label','kind','historical','segment')}
        for context in aliases:
            if not isinstance(context,str):continue
            self.groups[context]=dict(meta)
            for item in self.steps:
                if item.get('_context')==context:
                    item.update(_group=group,_label=meta.get('label') or '',
                        _kind=meta.get('kind') or '',_historical=bool(meta.get('historical')))
        # Several incoming parents may form one model batch. Fold preliminary
        # read facts only; separate send task/segment confirmations stay intact.
        kept={}
        for item in list(self.steps):
            if item.get('_group')!=group or item['_phase'] not in {'messages_saved','history','voice'}:continue
            phase=item['_phase'];prior=kept.get(phase)
            if phase=='voice':continue  # Distinct voice parents are separate actual calls.
            if prior is None:kept[phase]=item;continue
            if item['state']=='done':prior.update(state='done',description=item.get('description',''))
            for key,value in list(self.latest.items()):
                if value is item:self.latest[key]=prior
            if self._focus is item:self._focus=prior
            self.steps.remove(item)

    def model(self,*,historical=None):
        selected=[s for s in self.steps if historical is None or bool(s.get('_historical'))==historical]
        # Registry-free events keep the original API used by the OCR shell.
        if not self.groups:
            return [{k:v for k,v in s.items() if not k.startswith('_')} for s in selected]
        grouped={}
        for item in selected:grouped.setdefault(item.get('_group',item['_context']),[]).append(item)
        order=list(grouped)
        order.sort(key=lambda g:0 if g=='startup' else 2 if grouped[g][0].get('_historical') else 1)
        rank={phase:i for i,phase in enumerate((
            *STARTUP_PHASES,'invite_received','invite_prepare','search_empty','query_verified_and_network_lookup',
            'contact_result','invite_form_fields','verify_remark_shortcode','confirm_once','cleanup','invite_receipt',
            'invite_finished','bind','messages_saved','history','voice','model_queue','model_retry','decision',
            'reply_ready','send_conditions','send_delay','pre_send','send_submit','send_confirm','handoff',
            'handoff_closed','finished','sync_finished','stopped'))}
        result=[]
        for group in order:
            def phase_order(item):
                phase=item['_phase']
                if phase in {'send_conditions','send_delay','pre_send','send_submit','send_confirm'}:
                    segment=self.groups.get(item['_context'],{}).get('segment') or 1
                    return rank['send_conditions'],segment,rank[phase]
                return rank.get(phase,999),0,0
            items=sorted(grouped[group],key=phase_order)
            first=items[0];kind=first.get('_kind');label=first.get('_label') or '已绑定客户'
            title=''
            if kind in {'customer','batch','invite'}:
                title=f"客户 {label} · {'加好友' if kind=='invite' else '消息处理'}"
                if first.get('_historical'):title='历史 · '+title
            elif kind=='reconcile':title=f'客户 {label} · 启动消息核对'
            elif group=='startup':title='启动检查'
            elif kind=='system':title='运行状态'
            for index,item in enumerate(items):
                row={k:v for k,v in item.items() if not k.startswith('_')}
                row.update(groupId=group,groupTitle=title if index==0 else '')
                segment=self.groups.get(item['_context'],{}).get('segment')
                if segment and item['_phase'] in {'send_conditions','send_delay','pre_send','send_submit','send_confirm'}:
                    row['title']+=f'（第 {segment} 段）'
                row['focus']=bool(item is self._focus and (item['state'] in {'current','waiting'} or (item['state']=='error' and item['_phase']=='stopped')))
                result.append(row)
        return result
