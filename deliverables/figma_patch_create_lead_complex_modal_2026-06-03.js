// Figma use_figma patch script
// Target file: https://www.figma.com/design/MMe3OkaLN9UM5jNOGEy9aG
// Purpose: add corrected complex "新增客户" modal frame.
// Status: prepared but not uploaded because Figma MCP Starter plan tool-call limit blocked writes on 2026-06-03.

const pages = figma.root.children;
let page = pages.find((p) => p.name === "02 Components & States") || pages.find((p) => p.name.includes("Components")) || pages[0];
await figma.setCurrentPageAsync(page);

const bold = { family: "Inter", style: "Bold" };
const regular = { family: "Inter", style: "Regular" };
await figma.loadFontAsync(bold);
await figma.loadFontAsync(regular);

const hex = (h) => {
  const s = h.replace("#", "");
  return {
    r: parseInt(s.slice(0, 2), 16) / 255,
    g: parseInt(s.slice(2, 4), 16) / 255,
    b: parseInt(s.slice(4, 6), 16) / 255,
  };
};
const solid = (h, opacity = 1) => [{ type: "SOLID", color: hex(h), opacity }];

const maxX = page.children.reduce((m, n) => Math.max(m, n.x + n.width), 0);
const root = figma.createFrame();
root.name = "Modal / 新增客户 - 复杂补正版";
root.x = maxX + 120;
root.y = 40;
root.resize(900, 920);
root.fills = solid("#E6E6E6");
root.clipsContent = false;
page.appendChild(root);

function rect(parent, name, x, y, w, h, fill, stroke, radius = 8) {
  const r = figma.createRectangle();
  r.name = name;
  r.x = x;
  r.y = y;
  r.resize(w, h);
  r.fills = solid(fill);
  if (stroke) r.strokes = solid(stroke);
  r.strokeWeight = stroke ? 1 : 0;
  r.cornerRadius = radius;
  parent.appendChild(r);
  return r;
}

function line(parent, x, y, w) {
  const l = figma.createLine();
  l.x = x;
  l.y = y;
  l.resize(w, 0);
  l.strokes = solid("#E7EBEF");
  l.strokeWeight = 1;
  parent.appendChild(l);
  return l;
}

function text(parent, name, value, x, y, size = 14, font = regular, color = "#171B22", w = 200, h = 24) {
  const t = figma.createText();
  t.name = name;
  t.fontName = font;
  t.characters = value;
  t.fontSize = size;
  t.lineHeight = { unit: "PIXELS", value: Math.round(size * 1.45) };
  t.fills = solid(color);
  t.x = x;
  t.y = y;
  t.resize(w, h);
  parent.appendChild(t);
  return t;
}

function input(parent, name, x, y, w, h, value = "", placeholder = false) {
  rect(parent, `${name} / input`, x, y, w, h, "#FFFFFF", "#D8DEE6", 8);
  if (value) text(parent, `${name} / value`, value, x + 16, y + 13, 14, regular, placeholder ? "#8F98A4" : "#171B22", w - 32, 22);
}

function button(parent, label, x, y, w, h, kind = "secondary") {
  const fill = kind === "primary" ? "#4A6EA5" : "#FFFFFF";
  const stroke = kind === "primary" ? "#4A6EA5" : "#D8DEE6";
  const color = kind === "primary" ? "#FFFFFF" : "#171B22";
  rect(parent, `Button / ${label}`, x, y, w, h, fill, stroke, 8);
  const labelNode = text(parent, `Button text / ${label}`, label, x, y + 11, 14, bold, color, w, 22);
  labelNode.textAlignHorizontal = "CENTER";
}

text(root, "Page title", "组件与状态交付", 0, 0, 24, bold, "#171B22", 300, 36);
text(root, "Frame subtitle", "Modal / 新增客户 - 复杂补正版", 0, 40, 18, regular, "#8F98A4", 360, 28);

const modalX = 24;
const modalY = 96;
const modalW = 760;
const modalH = 780;
rect(root, "Modal container / 新增客户复杂版", modalX, modalY, modalW, modalH, "#FFFFFF", "#D8DEE6", 8);

text(root, "Modal title", "新增客户", modalX + 28, modalY + 28, 20, bold, "#171B22", 200, 32);

rect(root, "Alert / 重复手机号强提示", modalX + 28, modalY + 78, modalW - 56, 88, "#FFF0EC", "#F4B9AE", 8);
text(root, "Alert title", "该手机号已存在，不能重复新建", modalX + 48, modalY + 98, 16, bold, "#B85C4F", 360, 26);
text(root, "Alert desc", "已重复录入 3 次，本次备注将追加到原线索。", modalX + 48, modalY + 130, 14, regular, "#68717D", 420, 22);
button(root, "查看原线索", modalX + modalW - 166, modalY + 104, 112, 36, "secondary");

text(root, "Label / 客户名称", "客户名称 *", modalX + 28, modalY + 190, 13, bold, "#68717D", 180, 20);
input(root, "客户名称", modalX + 28, modalY + 216, modalW - 56, 44, "王先生");

text(root, "Label / 手机", "手机 *", modalX + 28, modalY + 286, 13, bold, "#68717D", 180, 20);
input(root, "手机 1", modalX + 28, modalY + 312, modalW - 192, 44, "13899996678");
button(root, "删除", modalX + modalW - 148, modalY + 312, 120, 44, "secondary");
button(root, "+ 添加手机号", modalX + 28, modalY + 366, 132, 36, "secondary");

text(root, "Label / 微信", "微信", modalX + 28, modalY + 426, 13, bold, "#68717D", 180, 20);
input(root, "微信 1", modalX + 28, modalY + 452, modalW - 192, 44, "wx_car_2026");
button(root, "删除", modalX + modalW - 148, modalY + 452, 120, 44, "secondary");
button(root, "+ 添加微信", modalX + 28, modalY + 506, 120, 36, "secondary");

text(root, "Label / 邮箱", "邮箱", modalX + 28, modalY + 566, 13, bold, "#68717D", 180, 20);
input(root, "邮箱", modalX + 28, modalY + 592, modalW - 56, 44, "客户邮箱，可选", true);

text(root, "Label / 备注", "备注", modalX + 28, modalY + 660, 13, bold, "#68717D", 180, 20);
input(root, "备注", modalX + 28, modalY + 686, modalW - 56, 72, "客户说预算 10 万左右，想看 SUV，周末方便到店。");

line(root, modalX + 28, modalY + modalH - 84, modalW - 56);
button(root, "取消", modalX + modalW - 308, modalY + modalH - 56, 76, 36, "secondary");
button(root, "保存", modalX + modalW - 216, modalY + modalH - 56, 76, 36, "secondary");
button(root, "保存并继续新增", modalX + modalW - 124, modalY + modalH - 56, 124, 36, "primary");

text(root, "Delivery note", "补正说明：该 frame 覆盖此前误传到 Figma 的简化新增客户弹窗；前端以本复杂版为准实现。", 24, 892, 13, regular, "#68717D", 760, 24);

return {
  createdNodeIds: [root.id],
  page: page.name,
  frameName: root.name,
  message: "已新增复杂版新增客户弹窗补正版 frame",
};
