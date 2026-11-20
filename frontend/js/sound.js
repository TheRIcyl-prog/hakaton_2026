// Звуки синтезируем WebAudio (без файлов), голос — встроенный синтез речи браузера (ru-RU, работает офлайн).
// Звук и голос включаются отдельно; выбор запоминается в браузере для каждого экрана (АРМ / стена).
let ctx = null;
let soundOn = false;
let voiceOn = false;
let unlocked = false;
let volume = 0.8;
let voice = null;
let scope = "arm";

const KEY = () => `mi-dashboard:sound:${scope}`;

// Читает сохранённый выбор. defaults — что включено при первом запуске на этом экране.
export function loadPrefs(name, defaults) {
  scope = name;
  let p = defaults;
  try { p = { ...defaults, ...JSON.parse(localStorage.getItem(KEY()) || "{}") }; } catch { /* приватный режим */ }
  soundOn = !!p.sound;
  voiceOn = !!p.voice;
  return { sound: soundOn, voice: voiceOn };
}

function savePrefs() {
  try { localStorage.setItem(KEY(), JSON.stringify({ sound: soundOn, voice: voiceOn })); } catch { /* приватный режим */ }
}

// Браузер разрешает звук только после клика — вызывать из обработчика нажатия
export function unlock() {
  try {
    ctx = ctx || new (window.AudioContext || window.webkitAudioContext)();
    if (ctx.state === "suspended") ctx.resume();
  } catch { ctx = null; }
  unlocked = true;
  pickVoice();
  if ("speechSynthesis" in window) speechSynthesis.onvoiceschanged = pickVoice;
}

export function isUnlocked() { return unlocked; }
export function isSound() { return soundOn; }
export function isVoice() { return voiceOn; }
export function hasVoice() { return !!voice; }

export function setSound(v) { soundOn = !!v; savePrefs(); }
export function setVoice(v) {
  voiceOn = !!v;
  if (!v && "speechSynthesis" in window) speechSynthesis.cancel();
  savePrefs();
}

function pickVoice() {
  if (!("speechSynthesis" in window)) return;
  const ru = speechSynthesis.getVoices().filter((v) => v.lang && v.lang.toLowerCase().startsWith("ru"));
  voice = ru.find((v) => /irina|svetlana|dariya|milena/i.test(v.name)) || ru.find((v) => /google/i.test(v.name)) || ru[0] || null;
}

const canPlay = () => soundOn && ctx;

function tone(freq, start, dur, { type = "sine", gain = 1, slideTo = null } = {}) {
  if (!ctx) return;
  const t0 = ctx.currentTime + start;
  const osc = ctx.createOscillator();
  const g = ctx.createGain();
  osc.type = type;
  osc.frequency.setValueAtTime(freq, t0);
  if (slideTo) osc.frequency.linearRampToValueAtTime(slideTo, t0 + dur);
  const v = Math.max(0.0001, volume * gain * 0.35);
  g.gain.setValueAtTime(0.0001, t0);
  g.gain.exponentialRampToValueAtTime(v, t0 + 0.015);
  g.gain.exponentialRampToValueAtTime(0.0001, t0 + dur);
  osc.connect(g).connect(ctx.destination);
  osc.start(t0);
  osc.stop(t0 + dur + 0.02);
}

// Тревожный двойной бип ×3 (громче, если loud)
export function playCritical(loud = false) {
  if (!canPlay()) return;
  const g = loud ? 1.6 : 1;
  for (let i = 0; i < 3; i++) {
    const s = i * 0.42;
    tone(988, s, 0.16, { type: "square", gain: 0.55 * g });
    tone(740, s + 0.19, 0.16, { type: "square", gain: 0.55 * g });
  }
}

// Мягкий «динь-дон» — назначен на координаторов
export function playCoordinators() {
  if (!canPlay()) return;
  tone(880, 0, 0.5, { gain: 0.9 });
  tone(659, 0.22, 0.7, { gain: 0.8 });
}

// Восходящий аккорд — решён
export function playResolved() {
  if (!canPlay()) return;
  [523, 659, 784, 1047].forEach((f, i) => tone(f, i * 0.11, 0.45, { type: "triangle", gain: 0.8 }));
}

// Тихий щелчок — прочие изменения
export function playTick() {
  if (!canPlay()) return;
  tone(1400, 0, 0.05, { type: "triangle", gain: 0.25 });
}

export function speak(text, { delay = 0, urgent = false } = {}) {
  if (!voiceOn || !unlocked || !("speechSynthesis" in window) || !voice) return;
  setTimeout(() => {
    const u = new SpeechSynthesisUtterance(text);
    u.voice = voice;
    u.lang = voice.lang;
    u.rate = urgent ? 1.08 : 1.0;
    u.volume = Math.min(1, volume + 0.1);
    speechSynthesis.speak(u);
  }, delay);
}

// Две кнопки «Звук» и «Голос» в шапке (АРМ и стена). onChange — после переключения.
export function bindToggles(soundBtn, voiceBtn, onChange) {
  const paint = () => {
    const waiting = !unlocked && (soundOn || voiceOn);
    soundBtn.classList.toggle("on", soundOn);
    soundBtn.classList.toggle("wait", waiting && soundOn);
    soundBtn.textContent = soundOn ? "🔔 Звук" : "🔕 Звук";
    soundBtn.title = waiting ? "Кликните по странице — браузер включит звук после нажатия" : (soundOn ? "Звук тревог включён" : "Звук тревог выключен");
    voiceBtn.classList.toggle("on", voiceOn);
    voiceBtn.classList.toggle("wait", waiting && voiceOn);
    voiceBtn.classList.toggle("na", unlocked && !voice);
    voiceBtn.textContent = voiceOn ? "🗣 Голос" : "🤐 Голос";
    voiceBtn.title = unlocked && !voice ? "В системе нет русского голоса — голос недоступен"
      : waiting ? "Кликните по странице — браузер включит голос после нажатия" : (voiceOn ? "Голос включён" : "Голос выключен");
  };
  soundBtn.onclick = () => {
    if (!unlocked) unlock();
    setSound(!soundOn);
    if (soundOn) playTick();
    paint(); onChange && onChange();
  };
  voiceBtn.onclick = () => {
    if (!unlocked) unlock();
    setVoice(!voiceOn);
    if (voiceOn) setTimeout(() => speak("Голос включён"), 200);
    paint(); onChange && onChange();
    setTimeout(paint, 800); // список голосов приходит не сразу
  };
  paint();
  return paint;
}
