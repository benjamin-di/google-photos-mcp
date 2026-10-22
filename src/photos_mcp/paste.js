// Paste into the sign-in window: Ctrl+V (Cmd+V on a Mac) types the clipboard's text into the remote page.
//
// Loaded beside noVNC's own vnc.html (connect.py adds one <script> tag; noVNC itself is unchanged). noVNC's keyboard
// handler swallows Ctrl+V on its canvas, so no paste event would ever fire. This module sees the key first (capture
// phase) and keeps it from noVNC without preventing the browser's default, so the browser fires a `paste` event with
// the text; then it releases the modifiers noVNC already pressed on the remote side and types the text key by key,
// paced, the way noVNC's own on-screen keyboard input does. If no paste event comes, it asks
// navigator.clipboard.readText() instead. The text is never logged or stored.
import UI from "./app/ui.js";
import KeyTable from "./core/input/keysym.js";
import keysyms from "./core/input/keysymdef.js";

const MAX_CHARS = 1000;
const PACE_MS = 20;
const FALLBACK_MS = 300;
const SPECIAL = { "\n": KeyTable.XK_Return, "\t": KeyTable.XK_Tab };

let typing = false;
let pending = null;
let fallbackAt = 0;

const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

function connected() {
  return UI.rfb && UI.rfb._rfbConnectionState === "connected" && !UI.rfb.viewOnly;
}

function editable(target) {
  return target instanceof HTMLElement &&
    (target.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName));
}

function releaseHeldKeys() {
  // Ctrl/Cmd (and anything else noVNC sent as pressed) goes up on the remote side first, so the text is typed as
  // plain keys. Going through noVNC's keyboard keeps its own record right: the person's later key-up is ignored.
  const keyboard = UI.rfb._keyboard;
  if (keyboard && keyboard._keyDownList) {
    if (keyboard._altGrArmed) keyboard._interruptAltGrSequence();
    for (const code of Object.keys(keyboard._keyDownList)) {
      keyboard._sendKeyEvent(keyboard._keyDownList[code], code, false);
    }
    return;
  }
  for (const [keysym, code] of [[KeyTable.XK_Control_L, "ControlLeft"], [KeyTable.XK_Control_R, "ControlRight"],
                                [KeyTable.XK_Super_L, "MetaLeft"], [KeyTable.XK_Super_R, "MetaRight"],
                                [KeyTable.XK_Shift_L, "ShiftLeft"], [KeyTable.XK_Alt_L, "AltLeft"]]) {
    UI.rfb.sendKey(keysym, code, false);
  }
}

async function type(text) {
  if (typing || !connected() || typeof text !== "string") return;
  text = text.replace(/\r\n?/g, "\n").replace(/\n+$/, "");
  const chars = Array.from(text).slice(0, MAX_CHARS);
  if (!chars.length) return;
  typing = true;
  try {
    releaseHeldKeys();
    for (const ch of chars) {
      if (!connected()) return;
      const cp = ch.codePointAt(0);
      const keysym = SPECIAL[ch] ?? (cp < 0x20 || cp === 0x7f ? 0 : keysyms.lookup(cp));  // other controls: skipped
      if (keysym) UI.rfb.sendKey(keysym, null);
      await sleep(PACE_MS);
    }
  } finally {
    typing = false;
  }
}

// While the text is being typed, no key reaches noVNC: a held Ctrl repeating its key-down would press Ctrl on the
// remote side again and turn the rest of the text into shortcuts. (Its key-up is ignored by noVNC: already released.)
for (const kind of ["keydown", "keyup"]) {
  document.addEventListener(kind, event => {
    if (typing && !editable(event.target)) {
      event.stopImmediatePropagation();
      event.preventDefault();
    }
  }, true);
}

document.addEventListener("keydown", event => {
  if (typing) return;
  const paste = (event.ctrlKey || event.metaKey) && !event.altKey && event.code === "KeyV";
  if (!paste || editable(event.target) || !connected()) return;
  // Not preventDefault: the browser's paste must still happen. Only noVNC must not see this key.
  event.stopPropagation();
  clearTimeout(pending);
  pending = setTimeout(() => {
    pending = null;
    if (navigator.clipboard && navigator.clipboard.readText) {
      fallbackAt = Date.now();
      navigator.clipboard.readText().then(type, () => {});
    }
  }, FALLBACK_MS);
}, true);

document.addEventListener("paste", event => {
  if (editable(event.target) || !connected()) return;
  clearTimeout(pending);
  pending = null;
  event.preventDefault();
  if (Date.now() - fallbackAt < 5000) return;  // a late paste event after the fallback already typed the text
  type(event.clipboardData ? event.clipboardData.getData("text/plain") : "");
}, true);
