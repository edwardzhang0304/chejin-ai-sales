"""Controlled physical desktop for composer tests; never a Windows UAT claim.

Original PNGs remain unmodified. Derived history/input/S2 images are explicitly
synthetic, assembled from their pixels and a font. Production registration,
RapidOCR, parsing, target/context checks, input confirmation, journal, Enter
decision, cleanup, and send-result confirmation all run normally.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

from PIL import Image, ImageDraw, ImageFont

from test_dynamic_composer import sidecar, install_desktop, register, incident_inputs

REPLY = ("您好，10万预算的话，油车和电车各有侧重：家里能装充电桩、平时市区通勤多，电车用起来更省；"
         "经常跑长途或充电不方便，油车更省心。 按咱们10万预算，可以看丰田bz7 2026款 600 Pro，"
         "纯电，9.88万；或者丰田卡罗拉2019款，燃油，4.88万。 您平时主要是市区代步还是跑长途多？我帮您再缩小范围。")


def worker_imports():
    configured=os.environ.get("CHEJIN_COMPOSER_WORKER_SOURCE","")
    root=Path(configured) if configured else next(
        (p for p in Path(__file__).resolve().parents if (p/"worker-client/chejin_worker_client").is_dir()),None)
    if root is None:
        __import__("pytest").skip("requires the consuming Worker checkout for joint contract checks")
    sys.path.insert(0, str(root / "worker-client"))
    from chejin_worker_client.task_runner import _bind_worker_continuity_contract_to_send_guard
    from chejin_worker_client.action_journal import initialize_action_journal
    return _bind_worker_continuity_contract_to_send_guard, initialize_action_journal


def derived_frames(*, movement=185, reduction=240, new_kind="", reply=REPLY, final_customer=False):
    """Algorithm fixtures, NOT evidence of how real Windows WeChat scrolls."""
    calibration, paths = incident_inputs()
    original = Image.open(paths["before_input"]).convert("RGB")
    draft = Image.open(paths["after_input"]).convert("RGB")
    font = ImageFont.truetype(os.environ.get("CHEJIN_COMPOSER_TEST_FONT", "/System/Library/Fonts/STHeiti Light.ttc"), 14)
    blocks = [original.crop((310, a, 768, b)) for a, b in ((130,190),(238,299),(313,393),(407,469))]
    # Deliberately include a repeated message. Unique identity must come from
    # the full surviving tail, never from simply counting messages.
    blocks.append(blocks[0])

    def bubble(text, role="self"):
        lines, current = [], ""
        for character in text:
            if character == "\n" or font.getlength(current + character) > 285:
                lines.append(current); current = "" if character == "\n" else character
            else:
                current += character
        if current: lines.append(current)
        image = Image.new("RGB", (458, max(44, len(lines)*20 + 22)), (250,250,250))
        draw = ImageDraw.Draw(image)
        left = 80 if role == "self" else 60
        draw.rounded_rectangle((left,0,left+316,image.height-1), radius=5,
                               fill=(157,242,155) if role == "self" else (237,237,237))
        avatar = original.crop((722,130,758,166) if role == "self" else (320,407,356,443))
        image.paste(avatar, (412 if role == "self" else 10, 0))
        for i,line in enumerate(lines): draw.text((left+12,10+i*20),line,font=font,fill=(25,25,25))
        return image

    if final_customer:
        blocks[-1] = bubble("平时市区通勤比较多，想了解电车", "customer")

    def make(shift, input_top, typing=False, sent=False):
        frame = original.copy()
        draw = ImageDraw.Draw(frame)
        draw.rectangle((301,81,778,800), fill=(250,250,250))
        chat = Image.new("RGB", (478,input_top-81), (250,250,250))
        extra = None
        if typing and new_kind:
            if new_kind == "same": extra = blocks[-1]
            elif new_kind == "sales": extra = bubble("销售补充新的车辆信息", "self")
            elif new_kind == "customer": extra = bubble("先等等，我的预算改成十五万元了", "customer")
            else:
                extra = bubble("", "customer")
                ImageDraw.Draw(extra).rectangle((75,8,125,34),fill=(80,130,160))
            shift += extra.height + 20
        for i,block in enumerate(blocks): chat.paste(block, (9,100+105*i-shift-81))
        if sent:
            chat.paste(bubble(reply), (9,510-81))
        if extra is not None:
            chat.paste(extra, (9,input_top-extra.height-96))
        frame.paste(chat, (301,81))
        draw.rounded_rectangle((304,input_top,775,830), radius=9, fill=(250,250,250), outline=(224,224,224), width=2)
        # Restore the actual toolbar separately from the growing input.
        frame.paste(original.crop((305,801,774,829)),(305,801))
        if typing:
            if reply==REPLY:
                frame.paste(draft.crop((313,685,766,789)), (313,input_top+10))
            else:
                lines,current=[],""
                for char in reply:
                    if char=="\n" or font.getlength(current+char)>425:
                        lines.append(current);current="" if char=="\n" else char
                    else:current+=char
                if current:lines.append(current)
                visible_lines=max(1,(789-input_top-10)//20)
                for i,line in enumerate(lines[-visible_lines:]):
                    draw.text((320,input_top+10+i*20),line,font=font,fill=(25,25,25))
            frame.paste(draft.crop((710,794,773,829)), (710,794))
        return frame
    return calibration, {"before":make(0,700), "typing":make(movement,700-reduction,True),
                         "cleared":make(0,700), "sent":make(210,700,sent=True)}


class Desktop:
    """Replace only OS input/capture, foreground and pacing boundaries."""
    def __init__(self, monkeypatch, directory, calibration, frames, *, reply=REPLY, unknown=False, cleanup_fails=False):
        self.directory=directory; directory.mkdir(parents=True,exist_ok=True)
        self.calibration=calibration; self.frames=frames; self.reply=reply
        self.unknown=unknown; self.cleanup_fails=cleanup_fails
        self.draft=""; self.clipboard="original clipboard"; self.selected=False
        self.enter_count=0; self.keys=[]; self.captures=[]; self.clicked=[]
        self.geometry=install_desktop(monkeypatch,directory,calibration)
        monkeypatch.setattr(sidecar,"win32gui",SimpleNamespace(GetForegroundWindow=lambda:calibration["hwnd"]))
        monkeypatch.setattr(sidecar,"win32con",SimpleNamespace(VK_CONTROL=17,VK_RETURN=13,VK_BACK=8,VK_RIGHT=39))
        monkeypatch.setattr(sidecar,"capture_wechat",self.capture)
        monkeypatch.setattr(sidecar,"sendinput_unicode_unit",self.unicode_unit)
        monkeypatch.setattr(sidecar,"human_client_click",self.click)
        monkeypatch.setattr(sidecar,"key_press",self.key)
        monkeypatch.setattr(sidecar,"hotkey",self.hotkey)
        monkeypatch.setattr(sidecar,"clipboard_read",lambda:self.clipboard)
        monkeypatch.setattr(sidecar,"clipboard_copy",lambda text:setattr(self,"clipboard",text))
        monkeypatch.setattr(sidecar,"humanized_sleep_ms",lambda *a,**k:None)
        monkeypatch.setattr(sidecar,"humanized_action_sleep",lambda *a,**k:None)
        monkeypatch.setenv("WECHAT_WIN32_OCR_HUMANIZED_TYPO_ENABLED","0")

    def unicode_unit(self, unit):
        data=self.draft.encode("utf-16-le",errors="surrogatepass") + int(unit).to_bytes(2,"little")
        self.draft=data.decode("utf-16-le",errors="surrogatepass")

    def click(self, hwnd, x, y, *, bounds, expected_snapshot_id="", **kwargs):
        assert bounds[0] <= x <= bounds[2] and bounds[1] <= y <= bounds[3]
        self.clicked.append([x,y]); self.selected=False

    def hotkey(self, *keys):
        self.keys.append(list(keys))
        if keys[-1] == ord("A"): self.selected=True
        if keys[-1] == ord("C"):
            assert self.selected
            self.clipboard=self.draft

    def key(self, key):
        self.keys.append(key)
        if key == 13:
            assert self.draft == self.reply
            self.enter_count+=1
            self.draft=""; self.selected=False
        elif key == 8:
            if self.cleanup_fails: raise OSError("controlled physical Backspace failure")
            self.draft="" if self.selected else self.draft[:-1]
            self.selected=False
        elif key == 39: self.selected=False

    def capture(self, hwnd, *, artifact_dir=None, label="frame", **kwargs):
        state=("before" if self.unknown else "sent") if self.enter_count else "typing" if self.draft else "before"
        image=self.frames[state].copy()
        path=self.directory/f"{len(self.captures):03d}-{label}.png"
        image.save(path)
        layout=register(image,self.calibration,path)
        self.captures.append({"label":label,"path":str(path),"state":state,"layout":layout})
        return image,str(path)

    def expected_guard(self):
        # Initial committed identity is a fixture, bound by the real Worker
        # function. No corrected OCR/context is injected into Sidecar.
        binder,_=worker_imports()
        snapshot=sidecar.capture_send_fact_snapshot(self.calibration["hwnd"],target="CJMKZUTH",text=self.reply,
                                                   exact=True,artifact_dir=str(self.directory),label="fixture_checkpoint")
        observations=copy.deepcopy(snapshot["observations"])
        for i,item in enumerate(observations):
            item["_worker_stable_id"]=f"committed-fixture-{i}"
            item["_worker_identity_scope"]="committed"
        return binder(snapshot["send_context_guard"],observations,checkpoint={},checkpoint_comparison={},empty_welcome_baseline=False)

    def run(self):
        guard=self.expected_guard()
        _,initialize=worker_imports()
        journal=self.directory/"action.json"
        initialize(journal,action_kind="send",transaction_id="composer-test-action",conversation_id="synthetic",
                   items=[{"journal_item_id":"composer-test-action"}])
        result=sidecar.send_payload(self.calibration["hwnd"],{},target="CJMKZUTH",text=self.reply,exact=True,
                                   skip_send_rate_guard=True,artifact_dir=str(self.directory),expected_context_guard=guard,
                                   action_journal_path=str(journal))
        record={"fixture_kind":"derived pixels and controlled Windows boundaries; real OCR and production send chain",
                "physical_enter_calls":self.enter_count,"result":result,"captures":self.captures,
                "journal":json.loads(journal.read_text()),"clipboard_restored":self.clipboard=="original clipboard"}
        (self.directory/"result.json").write_text(json.dumps(record,ensure_ascii=False,indent=2))
        return result
