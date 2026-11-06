import ctypes
import math

from PyQt6.QtCore import QPointF, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPainter, QPen, QPolygonF
from PyQt6.QtWidgets import QWidget

from direction import angle_diff, wrap_180

# Visual hold/decay constants (seconds).
# "Fast attack, slow release": a blip appears at full brightness instantly,
# stays fully lit for FLASH_HOLD, then fades slowly over (HOLD - FLASH_HOLD).
# After it dies, a dim GHOST marker keeps the direction lit for several more
# seconds - enemy activity direction stays visible through intense fights.
BLIP_HOLD = 3.0        # footsteps / misc: total visibility
GUNSHOT_HOLD = 2.5     # gunshots fade a bit sooner (they repeat anyway)
SILENT_HOLD = 4.0      # silent-step threat lingers longest
FLASH_HOLD = 0.55      # full-brightness hold right after (re)appearing
GHOST_HOLD = 4.0       # dim direction residue after a blip dies
BLIP_TICK = 0.030      # decay timer interval
MERGE_SPAN_DEG = 14.0  # tighter merge: the arc follows a turning view faster

# Per-kind attention: how long a sound's direction keeps being monitored
# after its last event (1 s, matching the slow-release), and how far its
# azimuth may jump before it counts as a DIFFERENT source of the same kind
# (a real turn between two events is small; a source switch can be 180°).
ATTENTION_HOLD_TICKS = int(1.0 / BLIP_TICK)
ATTENTION_SAME_SOURCE_DEG = 45.0
SHIFT_INDICATOR_TICKS = int(0.9 / BLIP_TICK)   # offset arrow/degrees hold
APPEAR_MIN = 0.55      # minimum first-appearance brightness (fast, obvious)

# Sound kinds (must match direction.py).
KIND_OTHER = 0
KIND_FOOTSTEP = 1
KIND_GUNSHOT = 2
KIND_SELF = 3
KIND_SILENT = 4

# Elevation colour mixing (up = warm orange, down = teal-green), applied to
# the accent. Chosen to stay readable on a BLUE footstep accent.
UP_COLOR = QColor(255, 176, 64)
DOWN_COLOR = QColor(47, 216, 160)
SELF_COLOR = QColor(255, 255, 255)
OTHER_COLOR = QColor(255, 225, 77)    # misc noises: YELLOW
SILENT_COLOR = QColor(180, 76, 255)   # silent steps: PURPLE threat marker

# How strongly the elevation tint mixes into the accent.
ELEV_TINT = 0.9

# Distance ring layout: distance 0 (near) .. 1 (far) maps onto radii
# 0.34R .. 1.10R. The dashed guide rings sit at near/mid/far boundaries.
DIST_NEAR_R = 0.34
DIST_FAR_R = 1.10
DIST_GUIDES = (0.45, 0.72, 0.98)


class OverlayRadar(QWidget):
    positionChanged = pyqtSignal(int, int)   # committed position (persist to disk)
    positionPreview = pyqtSignal(int, int)   # live drag frames (UI readout only)

    def __init__(self):
        super().__init__()

        self.setWindowTitle("SoundRadar360")
        self.base_window_flags = (
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool
        )
        self.drag_enabled = False
        self.drag_start_global = None
        self.drag_start_window = None
        self._apply_window_flags()
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        self.resize(320, 320)

        # Defaults match the dashboard swatches: footsteps BLUE, gunshots RED
        # (user request for the Arena Breakout setup).
        self.accent_color = QColor("#4FA8FF")
        self.gunshot_color = QColor("#FF3B30")
        self.stroke_width = 6
        self.show_elevation = True
        self.show_gunshots = True
        self.blips = []
        self.ghosts = []   # dim "recent activity" direction markers
        # Per-kind ATTENTION tracker (replaces the old cross-kind rotation
        # consensus). While a sound of one kind keeps sounding, its
        # head-relative azimuth shifts only when the PLAYER turns; the
        # attention records that shift and moves the kind's marker with the
        # source for ATTENTION_HOLD_SEC after the last event, then expires.
        # Each kind is tracked independently - a gunshot's monitoring is
        # never disturbed by a footstep (no cross-kind interference).
        self._attention = {}     # kind -> {'az': last azimuth, 't': last tick}
        self._shift_log = []     # (tick, kind, shift) for the turn estimate
        self._phase_front_side = None   # anchored midline-crossing phase
        self._tick = 0

        self.decay_timer = QTimer(self)
        self.decay_timer.timeout.connect(self.decay_signal)

    def _apply_window_flags(self):
        flags = self.base_window_flags
        if not self.drag_enabled:
            flags |= Qt.WindowType.WindowTransparentForInput
        self.setWindowFlags(flags)

    def set_drag_enabled(self, enabled):
        enabled = bool(enabled)
        if self.drag_enabled == enabled:
            return

        was_visible = self.isVisible()
        pos = self.pos()
        self.drag_enabled = enabled
        self.drag_start_global = None
        self.drag_start_window = None
        self.setCursor(Qt.CursorShape.OpenHandCursor if enabled else Qt.CursorShape.ArrowCursor)
        self._apply_window_flags()
        self.move(pos)

        if was_visible:
            self.show()
            self.raise_()
            self._remove_win11_border()
        self.update()

    def showEvent(self, event):
        super().showEvent(event)
        self.decay_timer.start(int(BLIP_TICK * 1000))
        self._remove_win11_border()

    def hideEvent(self, event):
        super().hideEvent(event)
        self.decay_timer.stop()
        self.blips = []
        self.ghosts = []

    def _remove_win11_border(self):
        """
        Windows 11 applies rounded corners and a border/shadow to ALL windows,
        including frameless ones. This opts out via the DWM API.
        Safe on non-Windows - the try/except swallows it silently.
        """
        try:
            hwnd = int(self.winId())

            # 1. Disable rounded corners
            #    DWMWA_WINDOW_CORNER_PREFERENCE = 33, DWMWCP_DONOTROUND = 1
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, 33,
                ctypes.byref(ctypes.c_int(1)),
                ctypes.sizeof(ctypes.c_int)
            )

            # 2. Remove drop shadow / border glow
            class MARGINS(ctypes.Structure):
                _fields_ = [
                    ("cxLeftWidth",    ctypes.c_int),
                    ("cxRightWidth",   ctypes.c_int),
                    ("cyTopHeight",    ctypes.c_int),
                    ("cyBottomHeight", ctypes.c_int),
                ]
            ctypes.windll.dwmapi.DwmExtendFrameIntoClientArea(
                hwnd, ctypes.byref(MARGINS(0, 0, 0, 0))
            )
        except Exception:
            pass

    def set_accent_color(self, hex_color):
        self.accent_color = QColor(hex_color)
        self.update()

    def set_gunshot_color(self, hex_color):
        self.gunshot_color = QColor(hex_color)
        self.update()

    def set_stroke_width(self, width):
        self.stroke_width = width
        self.update()

    def set_show_elevation(self, enabled):
        self.show_elevation = bool(enabled)
        self.update()

    def set_show_gunshots(self, enabled):
        self.show_gunshots = bool(enabled)
        if not enabled:
            self.blips = [b for b in self.blips if b['kind'] != KIND_GUNSHOT]
        self.update()

    def mousePressEvent(self, event):
        if not self.drag_enabled or event.button() != Qt.MouseButton.LeftButton:
            return super().mousePressEvent(event)

        self.drag_start_global = event.globalPosition().toPoint()
        self.drag_start_window = self.pos()
        self.setCursor(Qt.CursorShape.ClosedHandCursor)
        event.accept()

    def mouseMoveEvent(self, event):
        if not self.drag_enabled or self.drag_start_global is None or self.drag_start_window is None:
            return super().mouseMoveEvent(event)

        delta = event.globalPosition().toPoint() - self.drag_start_global
        new_pos = self.drag_start_window + delta
        self.move(new_pos)
        self.positionPreview.emit(new_pos.x(), new_pos.y())
        event.accept()

    def mouseReleaseEvent(self, event):
        if not self.drag_enabled or event.button() != Qt.MouseButton.LeftButton:
            return super().mouseReleaseEvent(event)

        self.drag_start_global = None
        self.drag_start_window = None
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        pos = self.pos()
        self.positionChanged.emit(pos.x(), pos.y())
        event.accept()

    def _update_rotation(self):
        """Removed: direction monitoring is now per-kind attention (see
        _attention in __init__). Kept as a no-op so old call sites stay
        harmless."""
        return

    def decay_signal(self):
        self._tick += 1
        self._update_rotation()
        # Fast attack, slow release: keep the flash phase at full brightness,
        # then fade over a FIXED duration regardless of brightness (the per-
        # blip rate is set at creation/refresh); dead blips become dim ghosts
        # that linger so the "activity direction" stays marked.
        active = []
        for blip in self.blips:
            if blip.get('flash', 0.0) > 0.0:
                blip['flash'] -= BLIP_TICK
                active.append(blip)
                continue
            blip['aged'] = blip.get('aged', 0.0) + BLIP_TICK
            blip['life'] -= blip.get('decay', 0.5) * BLIP_TICK
            if blip['life'] > 0:
                active.append(blip)
            else:
                self.ghosts.append({
                    'az': blip['az'], 'dist': blip['dist'],
                    'kind': blip['kind'], 'life': 1.0,
                })
        self.blips = active

        alive = []
        for g in self.ghosts:
            g['life'] -= BLIP_TICK / GHOST_HOLD
            if g['life'] > 0:
                alive.append(g)
        if len(alive) > 40:
            alive.sort(key=lambda g: -g['life'])
            del alive[40:]
        self.ghosts = alive
        self.update()

    # ── Audio events (azimuth 360°, elevation, intensity, distance, kind) ──
    def update_audio_data(self, azimuth, elevation, intensity, distance, kind,
                          fb_amb=0.0):
        life = max(0.0, min(1.0, float(intensity)))
        if life <= 0.02:
            return
        if kind == KIND_GUNSHOT and not self.show_gunshots:
            return
        if kind == KIND_SELF:
            # Own sound: a quiet centre pulse, never a direction claim.
            self._pulse = life
            self.update()
            return
        # Fast appearance: first frame is already at least APPEAR_MIN bright.
        life = max(life, APPEAR_MIN)

        # ── Per-kind attention (1 s hold, renews while the sound continues) ─
        # While a sound of THIS kind keeps sounding, its head-relative
        # azimuth moves only when the player turns; the attention tracks
        # that shift and carries the kind's markers with the source so the
        # angle stays correct through the turn. A jump too large for a turn
        # (a different source of the same kind) resets the tracking instead
        # of dragging the old marker onto it. Other kinds are untouched.
        att = self._attention.get(int(kind))
        shift = 0.0
        if att is not None and (self._tick - att['t']) <= ATTENTION_HOLD_TICKS:
            delta = ((float(azimuth) - att['az'] + 180.0) % 360.0) - 180.0
            if abs(delta) <= ATTENTION_SAME_SOURCE_DEG:
                shift = delta
                for blip in self.blips:
                    # rear-resolved markers follow via the merge's rear
                    # equivalent (their lateral rate differs from a front
                    # source's), not via the uniform attention shift
                    if blip['kind'] == int(kind) \
                            and not blip.get('resolved_rear'):
                        blip['az'] = ((blip['az'] + delta + 180.0) % 360.0) - 180.0
            # else: new source of the same kind - just re-anchor below
        # The attention's own anchor is smoothed: a noisy pulse must not
        # jitter the tracked direction, but the sweep must not lag - 0.7 of
        # the new reading keeps the follow-up under ~1 event.
        new_anchor = float(azimuth)
        if att is not None:
            new_anchor = wrap_180(att['az'] * 0.30 + new_anchor * 0.70)
        self._attention[int(kind)] = {'az': new_anchor, 't': self._tick}

        # ── Midline-crossing phase tracker ─────────────────────────────
        # While a source sweeps around the player, its azimuth crosses the
        # front midline twice per circle (once truly front, once truly rear
        # - the audio cannot tell, but they ALTERNATE). Once any anchor
        # labels a crossing (the turn-based auto-resolution above, or a
        # bright sound's own cue), the phase is known: every later crossing
        # is pinned to the correct side - rear crossings show at 180°.
        for blip in self.blips:
            if blip['kind'] != int(kind):
                continue
            side = 1 if blip['az'] >= 0.0 else -1
            prev_side = blip.get('side')
            crossed = (prev_side is not None and side != prev_side
                       and abs(blip['az']) < 20.0)
            if crossed and self._phase_front_side is not None:
                # every crossing gets labelled from the anchored phase:
                # the front crossing approaches from _phase_front_side,
                # the rear crossing from the opposite side
                if prev_side == self._phase_front_side:
                    blip['az'] = 0.0
                    blip['resolved_rear'] = False
                else:
                    blip['az'] = 180.0
                    blip['resolved_rear'] = True
                blip['amb'] = False
                blip['resolved'] = True
            blip['side'] = side

        # ── Source-side correction (head-turn disambiguation) ───────────
        # Physics: turning the head right moves every source's bearing LEFT,
        # but a FRONT source's lateral angle SHRINKS toward the midline while
        # a REAR source's lateral angle GROWS toward the side. So once the
        # turn direction is known, a tracked source's lateral trend resolves
        # front vs rear - the one thing the game's audio cannot say directly.
        # The turn direction is estimated ONLY when >= 2 kinds' attention
        # shifts agree (the common cause is the player's turn); each kind's
        # own tracking stays independent otherwise.
        if abs(shift) >= 6.0:
            self._shift_log.append((self._tick, int(kind), shift))
        self._shift_log = [(t, k, s) for t, k, s in self._shift_log
                           if self._tick - t <= 12]     # ~0.36 s window
        votes = {}
        for _t, k, s in self._shift_log:
            if abs(s) >= 6.0:
                votes[k] = 1 if s > 0 else -1
        turn = None
        if len(votes) >= 2 and len(set(votes.values())) == 1:
            turn = -list(votes.values())[0]      # +1 = turned right
        if turn is not None:
            for blip in self.blips:
                if not blip.get('amb') or blip.get('resolved'):
                    continue
                az0 = blip.get('az_turn0')
                if az0 is None:
                    # snapshot the pre-shift angle when the turn is detected
                    blip['az_turn0'] = ((blip['az'] - shift + 180.0) % 360.0) - 180.0
                    continue
                d_abs = abs(blip['az']) - abs(az0)
                if d_abs * turn >= 0:
                    # lateral grew with the turn: the source is on the REAR
                    # hemisphere - pin the marker to the true rear bearing
                    # (from the blip's OWN angle, never the incoming event's)
                    blip['az'] = math.copysign(180.0 - abs(blip['az']), blip['az'])
                    blip['resolved_rear'] = True
                # lateral shrank: front hemisphere - the default was right
                blip['amb'] = False
                blip['az_turn0'] = None
                blip['resolved'] = True
                # anchor the midline-crossing phase for later sweeps
                s_side = 1 if blip['az'] >= 0.0 else -1
                self._phase_front_side = (s_side if not blip.get('resolved_rear')
                                          else -s_side)
        elif abs(shift) >= 8.0:
            # No turn estimate (single active kind): show the offset as an
            # arrow + degrees so the player reads it against their own turn.
            for blip in self.blips:
                if blip['kind'] == int(kind) and blip.get('amb'):
                    blip['shift'] = (shift, self._tick + SHIFT_INDICATOR_TICKS)

        for blip in self.blips:
            # angle_diff wraps at +-180 so a sound directly behind the player
            # refreshes one blip instead of two. Blips of different kinds do
            # not merge - a gunshot and a footstep at the same angle stay
            # separate so both stay readable.
            if blip['kind'] != kind:
                continue
            # A source resolved to the REAR hemisphere compares/updates
            # against the rear equivalent of the incoming (front-default)
            # azimuth, so it keeps merging instead of spawning a second mark.
            az_cmp = float(azimuth)
            merge_span = MERGE_SPAN_DEG
            if blip.get('resolved_rear'):
                az_cmp = math.copysign(180.0 - abs(float(azimuth)),
                                       float(azimuth))
                merge_span = 25.0
            if angle_diff(blip['az'], az_cmp) < merge_span:
                blip['life'] = max(blip['life'], life)
                blip['peak'] = max(blip['peak'], life)
                blip['flash'] = FLASH_HOLD   # re-flash on every new hit
                # Fixed fade duration: rate derived from the refreshed life.
                blip['decay'] = blip['life'] / max(blip.get('hold', BLIP_HOLD)
                                                   - FLASH_HOLD, 0.3)
                # Follow the newest position aggressively: the player's view
                # turns constantly, so old angles are stale after one chunk.
                blip['az'] = blip['az'] * 0.3 + az_cmp * 0.7
                blip['aged'] = 0.0
                blip['elev'] = blip['elev'] * 0.4 + float(elevation) * 0.6
                blip['dist'] = blip['dist'] * 0.4 + float(distance) * 0.6
                if not blip.get('resolved'):
                    blip['amb'] = bool(abs(float(fb_amb)) < 0.2)
                self.ghosts = [g for g in self.ghosts
                               if angle_diff(g['az'], blip['az']) >= 18.0]
                return
        self.ghosts = [g for g in self.ghosts
                       if angle_diff(g['az'], float(azimuth)) >= 18.0]
        hold = {KIND_GUNSHOT: GUNSHOT_HOLD,
                KIND_SILENT: SILENT_HOLD}.get(int(kind), BLIP_HOLD)
        self.blips.append({
            'az': float(azimuth),
            'elev': float(elevation),
            'life': life,
            'peak': life,
            'flash': FLASH_HOLD,
            'dist': max(0.0, min(1.0, float(distance))),
            'kind': int(kind),
            'hold': hold,
            'decay': life / max(hold - FLASH_HOLD, 0.3),
            'aged': 0.0,
            # Front/back ambiguity at emission: the game renders some sounds
            # (machine noise, dull footsteps) with lateral panning only, so
            # front vs rear is physically undecidable - the radar then shows
            # a faint MIRROR arc on the other side as an honest alternative.
            'amb': bool(abs(float(fb_amb)) < 0.2),
        })
        # Cap runaway blips (every 25ms chunk could add one in theory).
        if len(self.blips) > 30:
            self.blips.sort(key=lambda b: -b['life'])
            del self.blips[30:]
        self.update()

    # ── Painting ──────────────────────────────────────────────────────────
    def _layout(self):
        w, h = self.width(), self.height()
        center = QPointF(w / 2, h / 2)
        radius = min(w, h) / 2 * 0.78
        return center, radius

    def _arc_geometry(self, radius, distance):
        """Radius for an event at relative distance 0 (near) .. 1 (far):
        centre of the radar = right next to you, outer rings = far away."""
        dist = max(0.0, min(1.0, distance))
        return radius * (DIST_NEAR_R + (DIST_FAR_R - DIST_NEAR_R) * dist)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        if self.drag_enabled:
            painter.fillRect(self.rect(), QColor(255, 255, 255, 8))

        center, radius = self._layout()
        self._paint_guide(painter, center, radius)
        for ghost in self.ghosts:
            self._paint_ghost(painter, center, radius, ghost)
        for blip in self.blips:
            self._paint_blip(painter, center, radius, blip)
        self._paint_self_pulse(painter, center, radius)

    def _paint_ghost(self, painter, center, radius, ghost):
        """Dim residue of a dead blip: keeps the 'recent activity' direction
        marked for a few more seconds after the sound stopped. The per-kind
        attention already carried it through any turn while it was alive."""
        life = max(0.0, min(1.0, ghost.get('life', 0.0)))
        if life <= 0.01:
            return
        alpha = int(50 * life)
        az_eff = wrap_180(ghost.get('az', 0.0))
        r = self._arc_geometry(radius, ghost.get('dist', 0.5))
        qt_deg = 90.0 - az_eff
        pen = QPen(self._blip_color(ghost, alpha))
        pen.setWidth(max(2, self.stroke_width - 3))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setStyle(Qt.PenStyle.DashLine)   # historical info, not live
        pen.setDashPattern([5, 5])
        painter.setPen(pen)
        span = 34    # a sector hint, not a precise bearing
        start_16 = int((qt_deg - span / 2) * 16)
        span_16 = int(span * 16)
        rect = QRectF(center.x() - r, center.y() - r, r * 2, r * 2)
        painter.drawArc(rect, start_16, span_16)

    def _paint_guide(self, painter, center, radius):
        # Main compass circle
        pen = QPen(QColor(255, 255, 255, 34))
        pen.setWidth(2)
        painter.setPen(pen)
        painter.drawEllipse(center, radius, radius)

        # Distance guide rings (near / mid / far).
        pen.setColor(QColor(255, 255, 255, 16))
        pen.setStyle(Qt.PenStyle.DashLine)
        painter.setPen(pen)
        for g in DIST_GUIDES:
            r = radius * g
            painter.drawEllipse(center, r, r)
        pen.setStyle(Qt.PenStyle.SolidLine)

        # Direction ticks every 45° + compass letters. N = front (head-relative).
        tick_pen = QPen(QColor(255, 255, 255, 90))
        tick_pen.setWidth(2)
        painter.setPen(tick_pen)
        font = QFont("Segoe UI", 9)
        font.setBold(True)
        painter.setFont(font)
        labels = {0: "N", 90: "E", 180: "S", 270: "W"}
        for deg in range(0, 360, 45):
            rad = math.radians(deg)
            c, s = math.cos(rad), math.sin(rad)
            x1 = center.x() + c * (radius + 5)
            y1 = center.y() + s * (radius + 5)
            x2 = center.x() + c * (radius + 11)
            y2 = center.y() + s * (radius + 11)
            painter.drawLine(QPointF(x1, y1), QPointF(x2, y2))
        painter.setPen(QPen(QColor(255, 255, 255, 120)))
        for deg, letter in labels.items():
            rad = math.radians(deg)
            x = center.x() + math.cos(rad) * (radius + 18)
            y = center.y() + math.sin(rad) * (radius + 18)
            rect = QRectF(x - 8, y - 8, 16, 16)
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, letter)

        # Front marker: small triangle just above the N label - "this way is
        # forward" for the head-relative radar.
        front = QPolygonF([
            QPointF(center.x(), center.y() - radius - 22),
            QPointF(center.x() - 6, center.y() - radius - 12),
            QPointF(center.x() + 6, center.y() - radius - 12),
        ])
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(255, 255, 255, 150))
        painter.drawPolygon(front)
        painter.setBrush(Qt.BrushStyle.NoBrush)

        if self.drag_enabled:
            setup_pen = QPen(QColor(255, 255, 255, 120))
            setup_pen.setWidth(1)
            setup_pen.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(setup_pen)
            painter.drawEllipse(center, radius + 8, radius + 8)

    def _blip_color(self, blip, alpha):
        """Colour by kind: footstep = blue accent, gunshot = red,
        misc noise = yellow, silent step = purple. Elevation tint only on
        footsteps, so the distinct warning colours stay unambiguous."""
        kind = blip.get('kind', KIND_OTHER)
        if kind == KIND_GUNSHOT:
            base = QColor(self.gunshot_color)
        elif kind == KIND_FOOTSTEP:
            base = QColor(self.accent_color)
        elif kind == KIND_SILENT:
            base = QColor(SILENT_COLOR)
        else:
            base = QColor(OTHER_COLOR)

        if self.show_elevation and kind == KIND_FOOTSTEP:
            elev = max(-1.0, min(1.0, blip.get('elev', 0.0)))
            f = min(1.0, abs(elev)) * ELEV_TINT
            target = UP_COLOR if elev > 0 else DOWN_COLOR
            r = int(base.red() * (1 - f) + target.red() * f)
            g = int(base.green() * (1 - f) + target.green() * f)
            b = int(base.blue() * (1 - f) + target.blue() * f)
            base = QColor(r, g, b)
        base.setAlpha(alpha)
        return base

    def _paint_blip(self, painter, center, radius, blip):
        life = max(0.0, min(1.0, blip['life']))
        if life <= 0.01:
            return
        alpha = int(255 * life ** 0.55)
        kind = blip.get('kind', KIND_OTHER)
        aged = blip.get('aged', 0.0)
        # The per-kind attention shifts the blip's az as the source sweeps
        # during a turn - no further correction is applied here.
        az_eff = wrap_180(blip['az'])
        r = self._arc_geometry(radius, blip.get('dist', 0.5))
        qt_deg = 90.0 - az_eff

        # Fresh marks point precisely; aging marks widen into honest "sector"
        # hints (direction grows uncertain after the player may have turned).
        widening = min(30.0, aged * 12.0) if kind in (KIND_FOOTSTEP, KIND_OTHER) \
            else min(20.0, aged * 10.0)
        if kind == KIND_GUNSHOT:
            span = 42.0 + widening
        elif kind == KIND_SILENT:
            span = 32.0 + widening
        elif kind == KIND_FOOTSTEP:
            span = 14 + 20 * life + widening
        else:
            # Misc noises are deliberately small and thin - clearly
            # subordinate to footsteps/combat/threats at a glance.
            span = 9 + 8 * life + widening

        pen = QPen(self._blip_color(blip, alpha))
        if kind in (KIND_GUNSHOT, KIND_SILENT):
            pen.setWidth(self.stroke_width + 2)
        elif kind == KIND_OTHER:
            pen.setWidth(max(2, self.stroke_width - 3))
        else:
            pen.setWidth(self.stroke_width)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        if aged > 1.0:
            # Dashed = historical / approximate direction, not live tracking.
            pen.setStyle(Qt.PenStyle.DashLine)
            pen.setDashPattern([4, 4])
        painter.setPen(pen)

        start_16 = int((qt_deg - span / 2) * 16)
        span_16 = int(span * 16)
        rect = QRectF(center.x() - r, center.y() - r, r * 2, r * 2)
        painter.drawArc(rect, start_16, span_16)

        # Echo trail for the strongest recent events (repeated footsteps read
        # as a pulsing trail instead of a single blink).
        if blip.get('peak', 0.0) > 0.45 and life > 0.15 and aged <= 1.0:
            trail_pen = QPen(self._blip_color(blip, int(alpha * 0.32)))
            trail_pen.setWidth(max(2, self.stroke_width - 2))
            trail_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(trail_pen)
            trail_rect = QRectF(center.x() - r * 0.86, center.y() - r * 0.86,
                                r * 1.72, r * 1.72)
            painter.drawArc(trail_rect, start_16, span_16)

        # Silent step: "!" glyph so the threat marker is unmistakable.
        if kind == KIND_SILENT and life > 0.3 and aged <= 1.0:
            self._paint_alert_glyph(painter, center, blip, r, alpha, qt_deg)

        # Elevation: a +/- sign next to the blip (+ = above, - = below).
        # (Skipped on aging marks: their direction is already approximate.)
        if self.show_elevation and kind != KIND_SELF and aged <= 1.0:
            self._paint_elev_glyph(painter, center, blip, r, alpha, qt_deg)

        # Front/back ambiguity: a faint MIRROR arc on the opposite side is
        # the honest alternative. Near the midline the front and rear are
        # EQUALLY likely (the game carries no cue at all there) - the mirror
        # is drawn at near-full strength so the rear option is unmissable.
        if blip.get('amb') and kind != KIND_SELF and aged <= 0.5:
            mirror = wrap_180(az_eff + 180.0)
            mq = 90.0 - mirror
            m_alpha = 0.7 if abs(az_eff) < 25.0 else 0.35
            mpen = QPen(self._blip_color(blip, int(alpha * m_alpha)))
            mpen.setWidth(max(2, self.stroke_width - 3))
            mpen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(mpen)
            mstart = int((mq - span / 2) * 16)
            painter.drawArc(rect, mstart, span_16)

        # Head-turn offset indicator: the tracked source shifted (the player
        # turned) but no auto-resolution was possible - show the offset as a
        # white arc + degrees so the single-ear player reads it against the
        # direction of their own turn.
        shift_info = blip.get('shift')
        if shift_info and self._tick <= shift_info[1] and aged <= 1.0:
            sd = shift_info[0]
            fade = max(0.0, min(1.0,
                                (shift_info[1] - self._tick) / SHIFT_INDICATOR_TICKS))
            q_old = 90.0 - wrap_180(az_eff - sd)
            q_new = 90.0 - az_eff
            ind_pen = QPen(QColor(255, 255, 255, int(190 * fade)))
            ind_pen.setWidth(2)
            ind_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(ind_pen)
            ind_r = r + 7
            ind_rect = QRectF(center.x() - ind_r, center.y() - ind_r,
                              ind_r * 2, ind_r * 2)
            painter.drawArc(ind_rect, int(q_old * 16),
                            int((q_new - q_old) * 16))
            mid_ang = math.radians(90.0 - (az_eff - sd / 2.0))
            tx = center.x() + math.cos(mid_ang) * (ind_r + 11)
            ty = center.y() + math.sin(mid_ang) * (ind_r + 11)
            font = QFont("Segoe UI", 9)
            font.setBold(True)
            painter.setFont(font)
            painter.setPen(QPen(QColor(255, 255, 255, int(220 * fade))))
            painter.drawText(QRectF(tx - 18, ty - 9, 36, 18),
                             Qt.AlignmentFlag.AlignCenter, f"{abs(sd):.0f}°")

    def _paint_alert_glyph(self, painter, center, blip, r, alpha, qt_deg):
        mid = math.radians(90.0 - qt_deg)
        c, s = math.cos(mid), math.sin(mid)
        rr = r + 14
        x = center.x() + c * rr
        y = center.y() + s * rr
        font = QFont("Segoe UI", 11)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QPen(QColor(SILENT_COLOR.red(), SILENT_COLOR.green(),
                                   SILENT_COLOR.blue(), alpha)))
        rect = QRectF(x - 8, y - 8, 16, 16)
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "!")

    def _paint_elev_glyph(self, painter, center, blip, r, alpha, qt_deg):
        """Elevation as a +/- sign next to the blip: + = the sound is above
        the player, - = below."""
        elev = max(-1.0, min(1.0, blip['elev']))
        if abs(elev) < 0.25:
            return
        up = elev > 0.0
        mid = math.radians(90.0 - qt_deg)
        c, s = math.cos(mid), math.sin(mid)
        rr = r + 13
        x = center.x() + c * rr
        y = center.y() + s * rr
        color = UP_COLOR if up else DOWN_COLOR
        color = QColor(color)
        color.setAlpha(int(alpha * 0.95))
        font = QFont("Segoe UI", 11)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QPen(color))
        rect = QRectF(x - 8, y - 8, 16, 16)
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "+" if up else "-")

    def _paint_self_pulse(self, painter, center, radius):
        """Dim centre pulse when a suppressed own sound fired - confirms the
        suppression is working without claiming a direction."""
        pulse = getattr(self, '_pulse', 0.0)
        if pulse <= 0.01:
            return
        pulse = max(pulse - 0.35, 0.0)
        self._pulse = pulse
        r = radius * 0.28
        alpha = int(150 * pulse)
        pen = QPen(QColor(255, 255, 255, alpha))
        pen.setWidth(3)
        painter.setPen(pen)
        painter.drawEllipse(center, r, r)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(255, 255, 255, alpha))
        painter.drawEllipse(center, 3.5, 3.5)
        painter.setBrush(Qt.BrushStyle.NoBrush)
