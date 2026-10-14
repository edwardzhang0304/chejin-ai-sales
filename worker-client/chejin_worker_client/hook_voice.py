"""Two-phase native voice evidence and a durable, non-retrying transcriber.

Only a caller holding an authoritative customer binding may request capture.
This layer checks its exact peer and parent ID; it creates no customer binding
or AI admission. Transcript delivery into the original parent event belongs to
the business dispatcher, not a second incoming text event.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import re
import xml.etree.ElementTree as ET

from . import storage
from .hook_inbox import HookReceiveScope, decode_callback
from .hook_gateway import HookLocalError
from .models import utc_now_iso
from .shared_rules import hook_contract_rules as rules

VOICE_FIELDS = ("self_wxid", "account_epoch", "native_msg_id", "peer_wxid", "process_generation",
    "audio_sha256", "audio_bytes", "state", "attempted_at", "completed_at", "response_sha256", "text", "error_code")


@dataclass(frozen=True)
class HookVoiceAudio:
    scope: HookReceiveScope
    peer_wxid: str
    native_msg_id: str
    audio: bytes = field(repr=False)

    def __post_init__(self):
        if (not isinstance(self.scope, HookReceiveScope) or not rules.wxid(self.peer_wxid)
                or self.peer_wxid == self.scope.self_wxid or not isinstance(self.native_msg_id,str)
                or not re.fullmatch(r"[1-9][0-9]{0,19}",self.native_msg_id)
                or not isinstance(self.audio,bytes) or not self.audio.startswith(b"\x02#!SILK_V3")):
            raise ValueError("HOOK_VOICE_INPUT_INVALID")

    @property
    def sha256(self):
        return hashlib.sha256(self.audio).hexdigest()

    @property
    def key(self):
        return (self.scope.self_wxid,self.scope.account_epoch,self.native_msg_id)


def capture_voice_audio(scope: HookReceiveScope, peer_wxid: str, native_msg_id: str, *, max_audio_bytes: int, verified_metadata_xml: str | None = None):
    """Resolve metadata and later audio within the same retained receive scope.

    Incomplete two-phase evidence returns None, never a guessed transcript.
    All copies must agree on the parent length and audio hash. Raw evidence is
    verified on every read; neither a changed account nor a group can attach.
    """
    if (not rules.wxid(peer_wxid) or peer_wxid==scope.self_wxid or not isinstance(native_msg_id,str)
            or not re.fullmatch(r"[1-9][0-9]{0,19}",native_msg_id)
            or type(max_audio_bytes) is not int or not 0<max_audio_bytes<=8*1024*1024):
        raise ValueError("HOOK_VOICE_CAPTURE_SCOPE_INVALID")
    with storage.db_connection() as conn:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='hook_raw_inbox'").fetchone() is None:
            return None
        rows=conn.execute("SELECT raw_body,raw_digest FROM hook_raw_inbox WHERE self_wxid=? AND account_epoch=? AND process_generation=? ORDER BY receive_seq",
            (scope.self_wxid,scope.account_epoch,scope.process_generation)).fetchall()
    lengths,audios=set(),{}
    if verified_metadata_xml is not None:
        # Only the dispatcher may supply XML from the authenticated native-work
        # inventory for this exact authorized parent. It contains no audio.
        try:
            node=ET.fromstring(verified_metadata_xml).find('voicemsg')
            length=int(node.attrib['length'])
            if not 0<length<=max_audio_bytes:raise ValueError()
            lengths.add(length)
        except (ValueError,TypeError,ET.ParseError,AttributeError,KeyError):
            raise ValueError('HOOK_VOICE_METADATA_INVALID') from None
    for row in rows:
        raw=row['raw_body']
        if not isinstance(raw,bytes) or hashlib.sha256(raw).hexdigest()!=row['raw_digest']:
            raise ValueError("HOOK_RAW_EVIDENCE_CHANGED")
        event=decode_callback(raw,scope)
        if str(event.get('sendorrecv'))!='2':continue
        for message in event['msglist']:
            if str(message.get('msgtype'))!='34' or str(message.get('msgsvrid'))!=native_msg_id:continue
            if message.get('msg'):
                if (message.get('fromid')!=peer_wxid or message.get('toid')!=scope.self_wxid
                        or str(message.get('fromtype','1'))!='1'):
                    raise ValueError("HOOK_VOICE_PARENT_IDENTITY_CONFLICT")
                try:
                    root=ET.fromstring(message['msg'])
                    node=root.find('voicemsg')
                    value=node.attrib['length']
                    if not re.fullmatch(r'[1-9][0-9]*',value):raise ValueError()
                    length=int(value)
                except (ET.ParseError,KeyError,AttributeError,TypeError,ValueError):
                    raise ValueError("HOOK_VOICE_METADATA_INVALID") from None
                if length>max_audio_bytes:raise ValueError("HOOK_VOICE_AUDIO_TOO_LARGE")
                lengths.add(length)
            if message.get('voice_data'):
                # The tested audio supplement has empty from/to/fromtype. Its
                # authority comes from this exact scoped parent metadata, never
                # from an empty field or the currently selected conversation.
                encoded=message['voice_data']
                if (message.get('fromid') not in (None,'',peer_wxid)
                        or message.get('toid') not in (None,'',scope.self_wxid)
                        or (message.get('fromtype') not in (None,'') and str(message['fromtype'])!='1')
                        or any(isinstance(message.get(field),str) and message[field].endswith('@chatroom')
                            for field in ('fromid','toid','fromgid'))):
                    raise ValueError("HOOK_VOICE_AUDIO_IDENTITY_CONFLICT")
                if (not isinstance(encoded,str) or len(encoded)>max_audio_bytes*2
                        or not re.fullmatch(r'(?:[0-9a-fA-F]{2})+',encoded)):
                    raise ValueError("HOOK_VOICE_AUDIO_INVALID")
                audio=bytes.fromhex(encoded)
                if str(message.get('voice_len'))!=str(len(audio)) or not audio.startswith(b"\x02#!SILK_V3"):
                    raise ValueError("HOOK_VOICE_AUDIO_INVALID")
                audios[hashlib.sha256(audio).hexdigest()]=audio
    if len(lengths)>1 or len(audios)>1:raise ValueError("HOOK_VOICE_AUDIO_AMBIGUOUS")
    if not lengths or not audios:return None
    audio=next(iter(audios.values()))
    if lengths!={len(audio)}:raise ValueError("HOOK_VOICE_LENGTH_MISMATCH")
    return HookVoiceAudio(scope,peer_wxid,native_msg_id,audio)


def _ensure_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS hook_voice_attempts (
        self_wxid TEXT NOT NULL,account_epoch TEXT NOT NULL,native_msg_id TEXT NOT NULL,
        peer_wxid TEXT NOT NULL,process_generation TEXT NOT NULL,audio_sha256 TEXT NOT NULL,
        audio_bytes INTEGER NOT NULL,state TEXT NOT NULL,attempted_at TEXT NOT NULL,
        completed_at TEXT,response_sha256 TEXT,text TEXT,error_code TEXT,
        PRIMARY KEY(self_wxid,account_epoch,native_msg_id))""")
    if tuple(r['name'] for r in conn.execute('PRAGMA table_info(hook_voice_attempts)'))!=VOICE_FIELDS:
        raise RuntimeError("HOOK_VOICE_SCHEMA_UNSUPPORTED")


def process_voice_call_unresolved(process_generation: str) -> bool:
    """Unknown DLL work blocks all new active calls in this process generation.

    There is no timeout-based automatic clearing. A new WeChat process has a
    different generation; the old attempt remains immutable and is not retried.
    """
    with storage.db_connection() as conn:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='hook_voice_attempts'").fetchone() is None:
            return False
        _ensure_schema(conn)
        return conn.execute("SELECT 1 FROM hook_voice_attempts WHERE process_generation=? AND state IN ('transcribing','uncertain') LIMIT 1",
            (process_generation,)).fetchone() is not None


class HookVoiceTranscriber:
    def __init__(self, *, max_audio_bytes: int, max_text_bytes: int):
        if (type(max_audio_bytes) is not int or not 0<max_audio_bytes<=8*1024*1024
                or type(max_text_bytes) is not int or not 0<max_text_bytes<=2*1024*1024):
            raise ValueError("HOOK_VOICE_LIMIT_INVALID")
        self.max_audio_bytes,self.max_text_bytes=max_audio_bytes,max_text_bytes

    def _read(self,audio):
        with storage.db_connection() as conn:
            _ensure_schema(conn)
            row=conn.execute('SELECT * FROM hook_voice_attempts WHERE self_wxid=? AND account_epoch=? AND native_msg_id=?',audio.key).fetchone()
            if row is None:return None
            if row['peer_wxid']!=audio.peer_wxid or row['audio_sha256']!=audio.sha256 or row['audio_bytes']!=len(audio.audio):
                raise ValueError("HOOK_VOICE_ATTEMPT_EVIDENCE_CONFLICT")
            return dict(row)

    def saved_parent(self,scope,peer,native_id):
        with storage.db_connection() as conn:
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='hook_voice_attempts'").fetchone() is None:return None
            _ensure_schema(conn)
            row=conn.execute('SELECT * FROM hook_voice_attempts WHERE self_wxid=? AND account_epoch=? AND native_msg_id=?',(scope.self_wxid,scope.account_epoch,native_id)).fetchone()
            if row is None:return None
            if row['peer_wxid']!=peer:raise ValueError('HOOK_VOICE_PARENT_IDENTITY_CONFLICT')
            return dict(row)

    def _reserve(self,audio):
        with storage.db_connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            _ensure_schema(conn)
            conn.execute('INSERT INTO hook_voice_attempts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (*audio.key,audio.peer_wxid,audio.scope.process_generation,audio.sha256,len(audio.audio),
                 'transcribing',utc_now_iso(),None,None,None,None))
            conn.commit()  # Must precede the first byte sent to Voice2Text.

    def _finish(self,audio,state,*,response=None,text=None,error=None):
        with storage.db_connection() as conn:
            _ensure_schema(conn)
            changed=conn.execute("UPDATE hook_voice_attempts SET state=?,completed_at=?,response_sha256=?,text=?,error_code=? WHERE self_wxid=? AND account_epoch=? AND native_msg_id=? AND state='transcribing'",
                (state,utc_now_iso(),hashlib.sha256(response).hexdigest() if response is not None else None,
                 text,error,*audio.key)).rowcount
            if changed!=1:raise RuntimeError("HOOK_VOICE_RESULT_NOT_SAVED")
            conn.commit()
        return self._read(audio)

    def transcribe_once(self,gateway,process,audio,*,automatic_transcription_disabled):
        """No generic retry. Configuration check is explicit and caller supplied.

        Checking the native setting's evidence source and authorization of this
        customer belongs to the dispatcher. Neither has an optimistic default.
        """
        if not isinstance(audio,HookVoiceAudio) or len(audio.audio)>self.max_audio_bytes:
            raise ValueError("HOOK_VOICE_INPUT_INVALID")
        old=self._read(audio)
        if old is not None:return old  # Saved result/unknown/failed, never reclick.
        scope=audio.scope
        with gateway._locked(process,'hook_voice_transcribe') as lease:
            gateway._history_identity(process,scope)
            if not gateway.inspect().window_available_for_actions:
                raise HookLocalError('HOOK_VOICE_WINDOW_NOT_AVAILABLE')
            if automatic_transcription_disabled() is not True:
                raise HookLocalError('HOOK_VOICE_NATIVE_AUTO_SETTING_UNCONFIRMED')
            lease.raise_if_lost()
            gateway._history_identity(process,scope)
            # Concurrent duplicate dispatch may have completed while this call
            # waited for the same original account action lease.
            old=self._read(audio)
            if old is not None:return old
            self._reserve(audio)
            try:
                response=gateway._response('Voice2Text',{'voice_hex':audio.audio.hex().upper(),
                    'msgsvrid':audio.native_msg_id},uncertain_on_failure=True)
            except HookLocalError as exc:
                self._finish(audio,'uncertain',error=exc.code)
                raise
            except BaseException:
                # If storage itself fails, the durable transcribing state still
                # prevents another active call in this process after restart.
                self._finish(audio,'uncertain',error='HOOK_VOICE_CALL_INTERRUPTED')
                raise
            payload=response.payload
            if (type(payload.get('status')) is int and payload['status']==1
                    and isinstance(payload.get('text'),str) and payload['text'].strip()
                    and len(payload['text'].encode('utf-8'))<=self.max_text_bytes):
                return self._finish(audio,'transcribed',response=response.raw_body,text=payload['text'])
            if type(payload.get('status')) is int and payload['status']==0:
                return self._finish(audio,'failed',response=response.raw_body,error='HOOK_VOICE_TRANSCRIPTION_FAILED')
            self._finish(audio,'uncertain',response=response.raw_body,error='HOOK_VOICE_RESPONSE_INVALID')
            raise HookLocalError('HOOK_VOICE_RESPONSE_INVALID',outcome_unknown=True)
