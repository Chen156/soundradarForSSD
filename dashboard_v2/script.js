/**
 * Visual Audio Overlay - dashboard_v2 frontend
 * ═══════════════════════════════════════════════════════════════════════
 * Talks to the Python `Bridge` (main.py) over QWebChannel.
 *   JS → Python : window.bridge.method(arg)
 *   Python → JS : window.bridge.signal.connect(cb)
 *
 * Bridge methods used (already in main.py):
 *   start_radar, stop_radar, set_sensitivity(float), set_gain(float),
 *   set_freq_range(int low, int high), set_max_amplitude(float),
 *   apply_preset(str), set_monitor(int), set_accent_color(hex),
 *   set_stroke_width(int), save_profile(jsonStr), delete_profile(str),
 *   request_initial_data()
 *
 *   set_program(str)            - per-app capture target
 *   programsChanged(jsonStr)    - list of running audio programs
 *
 *   set_selected_preset(str)      - persist the dropdown choice to settings.json
 *   selectedPresetChanged(str)    - that choice, restored on load (name only)
 *   audioSettingsChanged(jsonStr) - the live sensitivity/gain/freq/max-amp, so the
 *                                   sliders follow a preset and come back on load
 * ═══════════════════════════════════════════════════════════════════════
 */

let radarActive = false;
let moveModeActive = false;

// Project links (opened in the real browser via bridge.open_url). The update
// banner overrides updateUrl when a specific release page is known.
const REPO_URL = "https://github.com/mike-s-zaugg/VisualAudioOverlay";
const FEEDBACK_URL = REPO_URL + "/issues/new/choose";
const CONTRIBUTE_URL = REPO_URL + "/blob/main/CONTRIBUTING.md";
const COFFEE_URL = "https://buymeacoffee.com/mikezaugg";
let updateUrl = REPO_URL + "/releases/latest";

function openExternal(url) {
    if (bridge && bridge.open_url) bridge.open_url(url);
}

// ── Bridge init ────────────────────────────────────────────────────────
function initBridge() {
    return new Promise((resolve) => {
        new QWebChannel(qt.webChannelTransport, function (channel) {
            window.bridge = channel.objects.bridge;

            bridge.statusChanged.connect(onStatusChanged);
            bridge.deviceChanged.connect(onDeviceChanged);
            bridge.profilesChanged.connect(onProfilesChanged);
            bridge.monitorsChanged.connect(onMonitorsChanged);
            bridge.presetsChanged.connect(onPresetsChanged);
            bridge.overlayPositionChanged.connect(onOverlayPositionChanged);
            // Optional new signals - only connect if the backend provides them.
            if (bridge.programsChanged) bridge.programsChanged.connect(onProgramsChanged);
            if (bridge.updateAvailable) bridge.updateAvailable.connect(onUpdateAvailable);
            if (bridge.appearanceChanged) bridge.appearanceChanged.connect(onAppearanceChanged);
            if (bridge.audioSettingsChanged) bridge.audioSettingsChanged.connect(onAudioSettingsChanged);
            if (bridge.selectedPresetChanged) bridge.selectedPresetChanged.connect(onSelectedPresetChanged);

            // Show the current version in the footer.
            if (bridge.get_app_version) {
                bridge.get_app_version(function (v) { setText("footer-version", "v" + v); });
            }

            bridge.request_initial_data();

            // The Program list only contains apps that are currently playing audio.
            // Re-enumerate whenever the user returns to this window (e.g. alt-tabs
            // back from the game), so a game launched after startup shows up without
            // having to Start the radar first.
            //
            // Skipped while any <select> popup is open - it is the popup itself
            // stealing and returning focus, and answering that churn with a
            // blocking COM enumeration is what grew the list. See isSelectOpen.
            window.addEventListener("focus", () => {
                if (isSelectOpen()) return;
                AR.refreshPrograms();
            });

            resolve();
        });
    });
}

// Backend found a newer GitHub release. Show the dismissible banner.
function onUpdateAvailable(version, url) {
    if (url) updateUrl = url;
    setText("update-banner-text", "Version " + version + " is available.");
    toggleClass("update-banner", "is-hidden", false);
}

// ── Signal handlers (Python → JS) ──────────────────────────────────────
function onStatusChanged(message, isActive) {
    radarActive = isActive;
    setText("status-text", message);
    syncToggleUI();
}

function onDeviceChanged(label) {
    // label is "Name  (Nch)" - split the channel suffix onto its own line
    const m = label.match(/^(.*?)\s*\((.*)\)\s*$/);
    if (m) { setText("device-name", m[1].trim()); setText("device-channels", m[2].trim()); }
    else { setText("device-name", label); setText("device-channels", ""); }
}

function onMonitorsChanged(jsonStr) {
    fillSelect("monitor-select", JSON.parse(jsonStr).map(m => ({ value: m.idx, label: m.name })));
}

function onPresetsChanged(jsonStr) {
    window._presets = JSON.parse(jsonStr);   // array of built-in preset names
    rebuildPresetSelects();
}

function onProfilesChanged(jsonStr) {
    window._profiles = JSON.parse(jsonStr);  // { name: {settings...} }
    rebuildPresetSelects();
}

// The preset/profile selected in the last session, restored from settings.json.
// Only records the name here - rebuildPresetSelects does the re-selecting, since
// the matching option may not exist in the dropdown yet.
function onSelectedPresetChanged(name) {
    window._savedPreset = name || "";
    rebuildPresetSelects();
}

function onProgramsChanged(jsonStr) {
    const progs = JSON.parse(jsonStr);
    // Diff-guard: refresh fires on dropdown-open and on every window focus, so
    // identical lists arrive repeatedly. Rebuilding a native <select> while its
    // popup is open makes QtWebEngine grow the rendered list (the runaway
    // dropdown bug). Skip the DOM rebuild entirely when nothing changed.
    const sig = progs.join("\u0000");
    if (sig === window._programsSig) return;
    window._programsSig = sig;

    // No preferred value: fillSelect keeps whatever the user has selected, and
    // falls back to the first option ("All (system audio)") if that program has
    // stopped playing and dropped off the list.
    fillSelect("program-select", [{ value: "all", label: "All (system audio)" },
        ...progs.map(p => ({ value: p, label: p }))]);
}

// Saved overlay appearance (thickness) restored from settings.json.
// Setting an input's value programmatically does NOT fire its oninput, so this never
// loops back into a re-save.
function onAppearanceChanged(jsonStr) {
    const a = JSON.parse(jsonStr);
    if (a.thickness != null) {
        const th = document.getElementById("thickness");
        if (th) th.value = a.thickness;
        setText("thickness-val", a.thickness + " px");
        setFill("thickness", a.thickness, 1, 20);
    }
    drawPreview();
}

// Move the audio sliders and their readouts to match the backend. Sent on load
// (the values restored from settings.json) and after a built-in preset is applied
// - the backend is already authoritative in both cases, and setting an input's
// value in JS does not fire its oninput, so this never loops back into the bridge.
// Keys are optional, so a caller can push a subset.
function onAudioSettingsChanged(jsonStr) {
    const p = JSON.parse(jsonStr);
    if (p.sensitivity != null) {
        const slider = Math.round(p.sensitivity * 10000);   // slider units = f * 10000
        setSliderValue("sensitivity", slider);
        setText("sensitivity-val", p.sensitivity.toFixed(4));
        setFill("sensitivity", slider);
    }
    if (p.gain != null) {
        const slider = Math.round(p.gain * 10);             // slider units = f * 10
        setSliderValue("gain", slider);
        setText("gain-val", p.gain.toFixed(1) + "x");
        setFill("gain", slider);
    }
    if (p.freq_low != null && p.freq_high != null) {
        const loEl = document.getElementById("freq-low");
        const hiEl = document.getElementById("freq-high");
        if (loEl && hiEl) {   // tuning UI removed in the auto-tuned build
            loEl.value = p.freq_low;
            hiEl.value = p.freq_high;
            setText("freq-val", `${p.freq_low}-${p.freq_high} Hz`);
            updateDualFill();
        }
    }
    if (p.max_amp != null) {
        const slider = Math.round(p.max_amp * 100);   // slider units = max_amp * 100
        setSliderValue("max-amp", slider);
        setText("max-amp-val", p.max_amp.toFixed(2));
        setFill("max-amp", slider);
    }
    // SSD fork: 3D-radar detection settings
    if (p.footstep_boost != null) {
        const slider = Math.round(p.footstep_boost * 100);
        setSliderValue("footstep-boost", slider);
        setText("footstep-boost-val", p.footstep_boost.toFixed(1) + "x");
        setFill("footstep-boost", slider);
    }
    if (p.front_bias != null) {
        const slider = Math.round(p.front_bias * 100);
        setSliderValue("front-bias", slider);
        setText("front-bias-val", p.front_bias.toFixed(2));
        setFill("front-bias", slider);
    }
    if (p.adaptive_gate != null) {
        const cb = document.getElementById("adaptive-gate");
        if (cb) cb.checked = !!p.adaptive_gate;
    }
    if (p.show_elevation != null) {
        const cb = document.getElementById("show-elevation");
        if (cb) cb.checked = !!p.show_elevation;
    }
    if (p.hrtf_mode != null) {
        const sel = document.getElementById("hrtf-mode");
        if (sel) sel.value = p.hrtf_mode;
    }
    // SSD fork: sound kinds / own-sound suppression
    if (p.show_gunshots != null) {
        const cb = document.getElementById("show-gunshots");
        if (cb) cb.checked = !!p.show_gunshots;
    }
    if (p.gunshot_color != null) {
        const gc = document.getElementById("gunshot-color");
        if (gc) gc.value = p.gunshot_color;
    }
    if (p.suppress_own_steps != null) {
        const cb = document.getElementById("suppress-own-steps");
        if (cb) cb.checked = !!p.suppress_own_steps;
    }
    if (p.suppress_own_shots != null) {
        const cb = document.getElementById("suppress-own-shots");
        if (cb) cb.checked = !!p.suppress_own_shots;
    }
    if (p.log_events != null) {
        const cb = document.getElementById("log-events");
        if (cb) cb.checked = !!p.log_events;
    }
}

function onOverlayPositionChanged(jsonStr) {
    const state = JSON.parse(jsonStr);
    moveModeActive = !!state.drag_enabled;
    setText("position-val", `${state.x}, ${state.y}`);
    syncMoveUI();
}

// ── Preset / profile dropdowns ─────────────────────────────────────────
// Built-in presets (read-only) + user profiles (addable/deletable) share
// the dropdown. See handoff.md: merging presets+profiles is the intended
// data model; true editable presets need a backend change.
function rebuildPresetSelects() {
    const presets = window._presets || [];
    const profiles = Object.keys(window._profiles || {});
    const opts = [
        ...presets.map(n => ({ value: n, label: n })),
        ...profiles.map(n => ({ value: n, label: n + "  ★" })),  // ★ = saved profile
    ];
    // Until the selection saved in settings.json has been restored, it outranks
    // whatever the dropdown currently shows: presets and profiles arrive as two
    // separate signals, so the first rebuild can land on an arbitrary entry
    // simply because the list holding the saved one has not arrived yet. A saved
    // name that no longer exists (a profile deleted since) falls back to the
    // current pick rather than dropping the dropdown to its first entry.
    const saved = window._presetRestored ? null : window._savedPreset;
    const keep = document.getElementById("preset-select")?.value;
    const want = (saved && opts.some(o => o.value === saved)) ? saved : keep;
    // The restore replay is registered as this select's settled hook, so it also
    // runs when the rebuild had to be deferred past an open popup.
    fillSelect("preset-select", opts, want);
}

// Latch the restore once the saved name is actually showing in the dropdown,
// which hands control of the selection back to the user (see rebuildPresetSelects).
//
// It deliberately does NOT re-apply the entry. A saved profile is a snapshot of
// the colour, thickness, program and monitor as they were when it was saved, so
// replaying it on every launch overwrote anything changed since. Those settings
// are already restored from settings.json, one value at a time. The label can
// therefore disagree with the sliders once you tune them by hand - that is
// intended: the sliders are the truth, the name is a bookmark.
function maybeRestorePreset() {
    if (window._presetRestored) return;
    const name = window._savedPreset;
    if (!name) return;
    const sel = document.getElementById("preset-select");
    if (!sel || sel.value !== name) return;   // not in the dropdown (yet)
    window._presetRestored = true;
}

// ── AR namespace (JS → Python) ─────────────────────────────────────────
window.AR = {
    toggleRadar() {
        if (radarActive) bridge.stop_radar();
        else bridge.start_radar();
        // UI syncs authoritatively via statusChanged
    },

    setSensitivity(val) {
        const f = val / 10000;
        setText("sensitivity-val", f.toFixed(4));
        setFill("sensitivity", val);
        bridge.set_sensitivity(f);
    },

    setGain(val) {
        const f = val / 10;
        setText("gain-val", f.toFixed(1) + "x");
        setFill("gain", val);
        bridge.set_gain(f);
    },

    setMaxAmp(val) {
        const f = val / 100;
        setText("max-amp-val", f.toFixed(2));
        setFill("max-amp", val);
        bridge.set_max_amplitude(f);
    },

    // ── SSD fork: 3D-radar detection settings ────────────────────────
    setAdaptiveGate(on) { if (bridge.set_adaptive_gate) bridge.set_adaptive_gate(!!on); },

    setFootstepBoost(val) {
        const f = val / 100;
        setText("footstep-boost-val", f.toFixed(1) + "x");
        setFill("footstep-boost", val);
        bridge.set_footstep_boost(f);
    },

    setFrontBias(val) {
        const f = val / 100;
        setText("front-bias-val", f.toFixed(2));
        setFill("front-bias", val);
        bridge.set_front_bias(f);
    },

    setShowElevation(on) { if (bridge.set_show_elevation) bridge.set_show_elevation(!!on); },

    setHrtfMode(value) { if (bridge.set_hrtf_mode) bridge.set_hrtf_mode(value); },

    // ── SSD fork: sound kinds / own-sound suppression ────────────────
    setShowGunshots(on) { if (bridge.set_show_gunshots) bridge.set_show_gunshots(!!on); },

    setGunshotColor(hex) { if (bridge.set_gunshot_color) bridge.set_gunshot_color(hex); },

    setSuppressOwnSteps(on) {
        if (bridge.set_suppress_own_steps) bridge.set_suppress_own_steps(!!on);
    },

    setSuppressOwnShots(on) {
        if (bridge.set_suppress_own_shots) bridge.set_suppress_own_shots(!!on);
    },

    setLogEvents(on) { if (bridge.set_log_events) bridge.set_log_events(!!on); },

    // Frequency is in real Hz (locked decision). Dual handles.
    setFreqRange() {
        const lowEl = document.getElementById("freq-low");
        const highEl = document.getElementById("freq-high");
        let low = parseInt(lowEl.value);
        let high = parseInt(highEl.value);
        if (low > high) { [low, high] = [high, low]; }   // keep ordered
        setText("freq-val", `${low}-${high} Hz`);
        updateDualFill();
        bridge.set_freq_range(low, high);
    },

    applyPreset(name) {
        if (!name) return;
        // Remember the choice so it survives a restart. Also stops a stale saved
        // name (e.g. a profile deleted since) from overriding this pick on the
        // next dropdown rebuild.
        window._presetRestored = true;
        if (bridge.set_selected_preset) bridge.set_selected_preset(name);
        const profiles = window._profiles || {};
        if (profiles[name]) { applyProfileValues(profiles[name]); return; }   // saved profile
        bridge.apply_preset(name);                                            // built-in preset
    },

    addPreset() {
        const name = (window.prompt("Name this preset:") || "").trim();
        if (!name) return;
        const data = {
            name,
            sensitivity: intVal("sensitivity", 50),
            gain: intVal("gain", 10),
            freq_low: intVal("freq-low", 150),
            freq_high: intVal("freq-high", 4000),
            max_amp: intVal("max-amp", 100),
            // SSD fork: 3D-radar settings
            footstep_boost: intVal("footstep-boost", 200),
            front_bias: intVal("front-bias", 0),
            adaptive_gate: !!document.getElementById("adaptive-gate")?.checked,
            show_elevation: !!document.getElementById("show-elevation")?.checked,
            hrtf_mode: strVal("hrtf-mode", "auto"),
            show_gunshots: !!document.getElementById("show-gunshots")?.checked,
            gunshot_color: strVal("gunshot-color", "#FF3B30"),
            suppress_own_steps: !!document.getElementById("suppress-own-steps")?.checked,
            suppress_own_shots: !!document.getElementById("suppress-own-shots")?.checked,
            log_events: !!document.getElementById("log-events")?.checked,
            preset: "Custom",
            // Richer profiles: also capture the target program, monitor and
            // overlay appearance, so a profile restores everything.
            program: strVal("program-select", "all"),
            monitor: intVal("monitor-select", 0),
            accent_color: strVal("accent-color", "#9751F2"),
            thickness: intVal("thickness", 6),
        };
        // Select the new profile as soon as it comes back in profilesChanged, by
        // re-arming the restore latch: it selects a name the moment its option
        // exists, which is exactly the problem here (the option does not exist
        // yet). Python selects it on its side in save_profile, so both agree.
        window._savedPreset = name;
        window._presetRestored = false;
        bridge.save_profile(JSON.stringify(data));   // emits profilesChanged
    },

    deletePreset() {
        const sel = document.getElementById("preset-select");
        const name = sel?.value;
        if (!name) return;
        if (!(window._profiles || {})[name]) {
            window.alert("Built-in presets can't be deleted - only saved presets (★).");
            return;
        }
        bridge.delete_profile(name);
    },

    setMonitor(idx) { bridge.set_monitor(parseInt(idx)); },

    setProgram(value) {
        // Backend method may not exist yet - guard it.
        if (bridge.set_program) bridge.set_program(value);
        else console.log("set_program not wired yet; selected:", value);
    },

    refreshPrograms() {
        // Re-enumerate live audio programs when the dropdown is opened, so the
        // game shows up even if it started playing after the app launched.
        if (bridge.refresh_programs) bridge.refresh_programs();
    },

    // ── Mono output was removed (listen-only radar) ───────────────────
    installCable() {
        if (bridge.install_vbcable) bridge.install_vbcable();
    },

    toggleMoveMode() {
        bridge.set_overlay_drag_enabled(!moveModeActive);
    },

    nudgeOverlay(dx, dy) {
        bridge.nudge_overlay(parseInt(dx), parseInt(dy));
    },

    resetOverlay() {
        bridge.reset_overlay_position();
    },

    setThickness(val) {
        setText("thickness-val", val + " px");
        setFill("thickness", val, 1, 20);
        bridge.set_stroke_width(parseInt(val));
        drawPreview();
    },

    // ── Footer support links + update banner ──────────────────────────
    openRepo() { openExternal(REPO_URL); },
    openFeedback() { openExternal(FEEDBACK_URL); },
    openContribute() { openExternal(CONTRIBUTE_URL); },
    openCoffee() { openExternal(COFFEE_URL); },
    openUpdate() { openExternal(updateUrl); },
    dismissUpdate() { toggleClass("update-banner", "is-hidden", true); },
};

// Apply a saved profile's values to the controls and push to Python. Note that
// each AR.* call below goes through the bridge, so the values land back in the
// profile via the auto-update path - harmless, since they are the profile's own
// values and the sync skips a write when nothing changed.
// Richer fields (program/monitor/mono/appearance) are applied only when the
// profile has them, so profiles saved by older versions still load fine and
// simply leave those settings as they are.
function applyProfileValues(p) {
    setSliderValue("sensitivity", p.sensitivity ?? 50);
    setSliderValue("gain", p.gain ?? 10);
    setSliderValue("max-amp", p.max_amp ?? 100);
    document.getElementById("freq-low").value = p.freq_low ?? 150;
    document.getElementById("freq-high").value = p.freq_high ?? 4000;
    AR.setSensitivity(intVal("sensitivity", 50));
    AR.setGain(intVal("gain", 10));
    AR.setMaxAmp(intVal("max-amp", 100));
    AR.setFreqRange();

    // SSD fork: 3D-radar settings (older profiles simply leave them as-is).
    if (p.footstep_boost != null) {
        setSliderValue("footstep-boost", p.footstep_boost);
        AR.setFootstepBoost(p.footstep_boost);
    }
    if (p.front_bias != null) {
        setSliderValue("front-bias", p.front_bias);
        AR.setFrontBias(p.front_bias);
    }
    if (p.adaptive_gate != null && bridge.set_adaptive_gate) {
        const cb = document.getElementById("adaptive-gate");
        if (cb) cb.checked = !!p.adaptive_gate;
        bridge.set_adaptive_gate(!!p.adaptive_gate);
    }
    if (p.show_elevation != null && bridge.set_show_elevation) {
        const cb = document.getElementById("show-elevation");
        if (cb) cb.checked = !!p.show_elevation;
        bridge.set_show_elevation(!!p.show_elevation);
    }
    if (p.hrtf_mode != null && bridge.set_hrtf_mode) {
        const sel = document.getElementById("hrtf-mode");
        if (sel) sel.value = p.hrtf_mode;
        bridge.set_hrtf_mode(p.hrtf_mode);
    }
    if (p.show_gunshots != null && bridge.set_show_gunshots) {
        const cb = document.getElementById("show-gunshots");
        if (cb) cb.checked = !!p.show_gunshots;
        bridge.set_show_gunshots(!!p.show_gunshots);
    }
    if (p.gunshot_color != null && bridge.set_gunshot_color) {
        const gc = document.getElementById("gunshot-color");
        if (gc) gc.value = p.gunshot_color;
        bridge.set_gunshot_color(p.gunshot_color);
    }
    if (p.suppress_own_steps != null && bridge.set_suppress_own_steps) {
        const cb = document.getElementById("suppress-own-steps");
        if (cb) cb.checked = !!p.suppress_own_steps;
        bridge.set_suppress_own_steps(!!p.suppress_own_steps);
    }
    if (p.suppress_own_shots != null && bridge.set_suppress_own_shots) {
        const cb = document.getElementById("suppress-own-shots");
        if (cb) cb.checked = !!p.suppress_own_shots;
        bridge.set_suppress_own_shots(!!p.suppress_own_shots);
    }
    if (p.log_events != null && bridge.set_log_events) {
        const cb = document.getElementById("log-events");
        if (cb) cb.checked = !!p.log_events;
        bridge.set_log_events(!!p.log_events);
    }

    if (p.program != null) {
        const sel = document.getElementById("program-select");
        if (sel) {
            // The saved game may not be running (yet) - inject its option so
            // the dropdown shows the choice; the backend falls back to system
            // audio with a status message until the program plays audio.
            if (!Array.from(sel.options).some(o => o.value === p.program)) {
                const opt = document.createElement("option");
                opt.value = p.program;
                opt.textContent = p.program;
                sel.appendChild(opt);
            }
            sel.value = p.program;
        }
        AR.setProgram(p.program);
    }

    if (p.monitor != null) {
        const sel = document.getElementById("monitor-select");
        if (sel) sel.value = p.monitor;
        AR.setMonitor(p.monitor);
    }

    if (p.thickness != null) {
        setSliderValue("thickness", p.thickness);
        AR.setThickness(p.thickness);
    }
}

// ── Preview canvas - mirrors overlay.py rendering ──────────────────────
// overlay.py (SSD fork): compass circle + ticks + N marker, accent-coloured
// arc "blips" that shift inward (up) / outward (down) with elevation. Here we
// draw one static sample pair (front-up + right-level) so the user sees the
// chosen colour + thickness style.
function drawPreview() {
    const canvas = document.getElementById("preview-canvas");
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    const W = canvas.width, H = canvas.height;
    const cx = W / 2, cy = H / 2;
    const radius = Math.min(W, H) / 2 * 0.78;

    ctx.clearRect(0, 0, W, H);

    // Base circle (matches overlay: white @ ~13% alpha, 2px) + guide rings
    ctx.beginPath();
    ctx.arc(cx, cy, radius, 0, Math.PI * 2);
    ctx.strokeStyle = "rgba(255,255,255,0.18)";
    ctx.lineWidth = 2;
    ctx.stroke();
    ctx.setLineDash([3, 4]);
    ctx.beginPath();
    ctx.arc(cx, cy, radius * 0.74, 0, Math.PI * 2);
    ctx.arc(cx, cy, radius * 1.26, 0, Math.PI * 2);
    ctx.strokeStyle = "rgba(255,255,255,0.10)";
    ctx.stroke();
    ctx.setLineDash([]);

    // Ticks every 45° (overlay draws these too)
    ctx.strokeStyle = "rgba(255,255,255,0.35)";
    ctx.lineWidth = 2;
    for (let deg = 0; deg < 360; deg += 45) {
        const rad = deg * Math.PI / 180;
        ctx.beginPath();
        ctx.moveTo(cx + Math.cos(rad) * (radius + 5), cy + Math.sin(rad) * (radius + 5));
        ctx.lineTo(cx + Math.cos(rad) * (radius + 11), cy + Math.sin(rad) * (radius + 11));
        ctx.stroke();
    }

    const accent = document.getElementById("accent-color")?.value || "#4FA8FF";
    const thickness = parseInt(document.getElementById("thickness")?.value || 6);

    // Sample blip: front, elevated (inner ring)
    drawPreviewBlip(ctx, cx, cy, radius, 0, 0.75, accent, thickness, 0.95);
    // Sample blip: right, level (main ring)
    drawPreviewBlip(ctx, cx, cy, radius, 90, 0, accent, thickness, 0.8);
}

function drawPreviewBlip(ctx, cx, cy, radius, azimuth, elevation, accent, thickness, alpha) {
    // canvas 0° = +x, clockwise; overlay angle 0 = up (front). Convert.
    const centerDeg = -90 + azimuth;
    const r = radius * (1.0 - 0.26 * elevation);
    const spanDeg = 30;
    const start = (centerDeg - spanDeg / 2) * Math.PI / 180;
    const end = (centerDeg + spanDeg / 2) * Math.PI / 180;

    let stroke = accent;
    if (Math.abs(elevation) > 0.3) {
        const mix = elevation > 0 ? [255, 176, 64] : [47, 216, 160];
        const f = Math.min(1, Math.abs(elevation)) * 0.75;
        const c = hexToRgb(accent);
        stroke = `rgb(${Math.round(c[0] * (1 - f) + mix[0] * f)},` +
                 `${Math.round(c[1] * (1 - f) + mix[1] * f)},` +
                 `${Math.round(c[2] * (1 - f) + mix[2] * f)})`;
    }
    ctx.beginPath();
    ctx.arc(cx, cy, r, start, end);
    ctx.strokeStyle = stroke;
    ctx.globalAlpha = alpha;
    ctx.lineWidth = thickness;
    ctx.lineCap = "round";
    ctx.stroke();
    ctx.globalAlpha = 1;
}

function hexToRgb(hex) {
    const h = hex.replace("#", "");
    return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)];
}

// ── Dual-range fill geometry ───────────────────────────────────────────
function updateDualFill() {
    const wrap = document.getElementById("freq-range");
    if (!wrap) return;   // tuning UI removed in the auto-tuned build
    const fill = wrap.querySelector(".dualrange-fill");
    const min = +wrap.dataset.min, max = +wrap.dataset.max;
    let lo = +document.getElementById("freq-low").value;
    let hi = +document.getElementById("freq-high").value;
    if (lo > hi) [lo, hi] = [hi, lo];
    const pct = v => ((v - min) / (max - min)) * 100;
    fill.style.left = pct(lo) + "%";
    fill.style.right = (100 - pct(hi)) + "%";
}

// ── Small helpers ──────────────────────────────────────────────────────
function setText(id, txt) { const el = document.getElementById(id); if (el) el.textContent = txt; }
function toggleClass(id, cls, on) { const el = document.getElementById(id); if (el) el.classList.toggle(cls, on); }
function intVal(id, def) { const el = document.getElementById(id); return el ? parseInt(el.value) : def; }
function strVal(id, def) { const el = document.getElementById(id); return el ? el.value : def; }

// Rebuilds queued because their <select> was open at the time:
// { id: {opts, preferred} }.
const _pendingFills = {};

// Follow-up work to run once a select's options have actually landed in the DOM.
// Registered per id so a *deferred* fill still triggers what its caller expected
// to happen right after the rebuild.
const _fillSettled = { "preset-select": maybeRestorePreset };

// QtWebEngine draws <select> popups natively (that is why the open list is white
// instead of following our dark CSS - the popup is a Qt widget, not DOM). Two
// consequences, and both feed the runaway-dropdown bug:
//
//   1. Swapping the options while the popup is open makes Qt GROW the rendered
//      list instead of redrawing it, leaving a blank strip under the real
//      entries that gets taller with every rebuild.
//   2. The popup is a separate window, so opening it takes focus off the web
//      view and hands it straight back, repeatedly, for as long as it stays
//      open - and the focus handler answered every round with a blocking COM
//      enumeration whose reply rebuilt the very list being displayed.
//
// `document.activeElement` stays on the <select> for as long as its popup is up,
// so it answers both questions. It also stays there after the popup closes until
// focus moves on, which only makes this over-cautious, never wrong.
//
// 49b828f fixed one instance by diff-guarding the program list, but that only
// helps while the list is unchanged - any real change (a game starting or
// stopping audio) still rebuilt an open popup. This is the general fix and
// covers every dropdown, so it protects the preset list too.
function isSelectOpen(sel) {
    const active = document.activeElement;
    if (!active || active.tagName !== "SELECT") return false;
    return sel ? active === sel : true;
}

// Replace a <select>'s options. `preferred` is the value to end up selected;
// omit it to keep the current pick. See applySelectOptions for the fallbacks.
function fillSelect(id, opts, preferred) {
    const sel = document.getElementById(id);
    if (!sel) return;
    if (isSelectOpen(sel)) {
        // Flush on change AND blur: picking an entry closes the popup without
        // blurring, and clicking away blurs without a change. `once` is safe
        // because we only re-arm when another fill is queued.
        if (!(id in _pendingFills)) {
            sel.addEventListener("change", () => flushSelectFill(id, true), { once: true });
            sel.addEventListener("blur", () => flushSelectFill(id), { once: true });
        }
        _pendingFills[id] = { opts, preferred };
        return;
    }
    delete _pendingFills[id];
    applySelectOptions(sel, opts, preferred);
    _fillSettled[id]?.();
}

// Apply a rebuild that was deferred while the popup was open. `userPicked` marks
// the flush as triggered by the user choosing an entry, so their choice outranks
// whatever value the queued rebuild wanted.
function flushSelectFill(id, userPicked) {
    if (!(id in _pendingFills)) return;
    const { opts, preferred } = _pendingFills[id];
    delete _pendingFills[id];
    const sel = document.getElementById(id);
    if (!sel) return;
    applySelectOptions(sel, opts, userPicked ? sel.value : preferred);
    _fillSettled[id]?.();
}

// Which entry ends up selected: `preferred` when the caller named one, else the
// value already showing. Either way it is only assigned if the new list actually
// contains it - assigning a value with no matching <option> leaves selectedIndex
// at -1, which renders as an empty dropdown. Falling through to the first option
// is what a rebuilt select does on its own; the explicit line just makes that
// contract visible rather than inherited.
function applySelectOptions(sel, opts, preferred) {
    const want = preferred == null ? sel.value : String(preferred);
    sel.innerHTML = "";
    opts.forEach(o => {
        const opt = document.createElement("option");
        opt.value = o.value;
        opt.textContent = o.label;
        sel.appendChild(opt);
    });
    if (opts.some(o => String(o.value) === want)) sel.value = want;
    else if (opts.length) sel.selectedIndex = 0;
}

// Paint the filled portion of a single slider via the --fill CSS var.
function setFill(id, val, min, max) {
    const el = document.getElementById(id);
    if (!el) return;
    const lo = min ?? +el.min, hi = max ?? +el.max;
    const pct = ((val - lo) / (hi - lo)) * 100;
    el.style.setProperty("--fill", pct + "%");
}

function setSliderValue(id, value) {
    const el = document.getElementById(id);
    if (el) el.value = value;
}

function syncToggleUI() {
    const btn = document.getElementById("btn-toggle");
    const dot = document.getElementById("status-dot");
    if (btn) {
        btn.textContent = radarActive ? "End" : "Start";
        btn.classList.toggle("is-active", radarActive);
    }
    if (dot) {
        dot.classList.toggle("status-dot--on", radarActive);
        dot.classList.toggle("status-dot--off", !radarActive);
    }
}

function syncMoveUI() {
    const btn = document.getElementById("btn-move");
    if (!btn) return;
    btn.textContent = moveModeActive ? "Done" : "Move";
    btn.classList.toggle("is-active", moveModeActive);
}

// ── Bootstrap ──────────────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", function () {
    // Default program option until/unless backend sends a list
    fillSelect("program-select", [{ value: "all", label: "All (system audio)" }]);

    // Wire dual-range inputs (tuning UI removed in the auto-tuned build;
    // guard in case they return in a future debugging build).
    ["freq-low", "freq-high"].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.addEventListener("input", AR.setFreqRange);
    });

    // Initial paint of values/fills/preview
    AR_initLocal();

    initBridge().then(() => console.log("Visual Audio Overlay bridge ready."));
});

// Local (no-bridge) initial UI state so the panel looks right immediately.
function AR_initLocal() {
    setFill("sensitivity", 50); setFill("gain", 10);
    setFill("max-amp", 100); setFill("thickness", 6, 1, 20);
    setFill("footstep-boost", 200); setFill("front-bias", 0);
    setText("sensitivity-val", (50 / 10000).toFixed(4));
    setText("gain-val", "1.0x");
    setText("max-amp-val", "1.00");
    setText("thickness-val", "6 px");
    setText("footstep-boost-val", "2.0x");
    setText("front-bias-val", "0.00");
    setText("position-val", "0, 0");
    syncMoveUI();
    updateDualFill();
    setText("freq-val", "150-4000 Hz");
    drawPreview();
}
