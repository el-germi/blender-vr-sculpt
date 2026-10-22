"""Pie menus at the hand and twist knobs for values: the whole VR menu, with no panel.

Holding a face button opens a pie at that hand. A slice is picked by moving the hand toward it or
by flicking the thumbstick, and letting go of the button runs it. A slice with children opens its
own pie when the hand pushes on through it, or after a short dwell; pulling the hand back to where
the parent pie was centered goes back up. Letting go on such a slice keeps its pie open ("sticky")
for a trigger press to choose. Values are dials: point at one (or at a pie's center, if it has a
center dial) and twist the controller like a knob; the stick's X axis nudges a dial that is held.

The stick clicks are knobs too: hold one and twist that hand to scrub the mode's main value.

Everything here is state and math (no drawing, no bpy context), so it can be tested without a
headset: the operators feed it controller poses and stick values (operators.py), and
radial_draw.py draws it. Distances are in pie space, which is hand space (meters at viewer scale 1).
"""

import math
import time

from mathutils import Vector

# Pie geometry, hand-space meters.
RING_INNER = 0.022
RING_OUTER = 0.060
DEAD = 0.016  # the hand must move this far from the center before it points at a slice
PUSH = 0.072  # past this the hovered branch opens without waiting
BACK = 0.016  # this close to the parent pie's center goes back up a level
CLOSE_DISTANCE = 0.35  # a sticky pie closes when the hand goes this far from it

DWELL = 0.45  # seconds hovering a branch before it opens
STICK_DWELL = 0.3
TAP = 0.25  # a press released sooner than this, without picking anything, is a tap
STICKY_TIMEOUT = 10.0
STICK_PICK = 0.55  # stick deflection that picks a slice
STICK_REARM = 0.25  # the stick must come back under this before it picks in a new pie
STICK_RATE = math.radians(120.0)  # dial turn per second at full stick deflection
TWIST_GRAB = math.radians(7.0)  # twisting this far on a dial grabs it
MAX_SLICES = 8

CENTER = -1  # hover index of a pie's center dial


def twist(q_from, q_to):
    """Clockwise turn of a controller about its aim axis (local -Z, so as the user sees it looking along
    the controller), from q_from to q_to, in radians within (-pi, pi]."""
    d = q_from.inverted() @ q_to
    t = 2.0 * math.atan2(d.z, d.w)
    t = (t + math.pi) % (2.0 * math.pi) - math.pi
    return -t


def slice_index(x, y, n):
    """Slice under direction (x, y) for n slices: 0 at the top, then clockwise."""
    if n <= 0:
        return None
    a = math.degrees(math.atan2(x, y)) % 360.0  # clockwise from up
    return int(round(a / (360.0 / n))) % n


def slice_angle(i, n):
    """Direction (math angle, radians) of slice i's middle."""
    return math.radians(90.0 - i * 360.0 / n)


# --- values ------------------------------------------------------------------

class Dial:
    """A value turned like a knob. scale: 'LINEAR' (a half turn sweeps the range), 'LOG' (a quarter turn
    doubles or halves it, for sizes) or 'STEP' (integers, one step per STEP_ANGLE)."""

    LINEAR_SPAN = math.radians(180.0)
    LOG_DOUBLE = math.radians(90.0)
    STEP_ANGLE = math.radians(15.0)
    NOTCH = math.radians(10.0)  # haptic tick spacing for smooth dials

    def __init__(self, label, get, set, lo, hi, scale='LINEAR', fmt=None, gradient=None, length=False, span=None,
                 step_angle=None):
        self.label, self.get, self.set = label, get, set
        self.lo, self.hi, self.scale = lo, hi, scale
        self.fmt = fmt or ((lambda v: str(int(v))) if scale == 'STEP' else (lambda v: f"{v:.2f}"))
        self.gradient = gradient  # fraction -> rgb, for color dials
        self.length = length  # a hand-space size: the readout draws a ring this big
        self.span = span  # LINEAR: turn that sweeps the whole range (default a half turn)
        self.step_angle = step_angle or self.STEP_ANGLE  # STEP: turn per step
        self.ticks = None  # optional: a function returning fractions to mark on the gauge (keyframes)
        self.on_turn = None  # called with the dial after it changes the value
        self.start = None

    def text(self):
        return self.fmt(self.get())

    def fraction(self, v=None):
        v = self.get() if v is None else v
        if self.scale == 'LOG' and self.lo > 0:
            return min(max(math.log(max(v, self.lo) / self.lo) / math.log(self.hi / self.lo), 0.0), 1.0)
        span = self.hi - self.lo
        return 0.0 if span <= 0 else min(max((v - self.lo) / span, 0.0), 1.0)

    def value_at(self, start, angle):
        if self.scale == 'LOG':
            v = max(start, self.lo) * 2.0 ** (angle / self.LOG_DOUBLE)
        elif self.scale == 'STEP':
            v = int(round(start + angle / self.step_angle))
        else:
            v = start + (self.hi - self.lo) * angle / (self.span or self.LINEAR_SPAN)
        v = min(max(v, self.lo), self.hi)
        return int(v) if self.scale == 'STEP' else v

    def notch(self, angle):
        """Detent number for haptic ticks."""
        return int(math.floor(angle / (self.step_angle if self.scale == 'STEP' else self.NOTCH)))

    def begin(self):
        self.start = self.get()

    def turn(self, angle):
        """Set the value for a total turn of angle since begin(). True if it changed."""
        v = self.value_at(self.start, angle)
        if v != self.get():
            self.set(v)
            if self.on_turn is not None:
                self.on_turn(self)
            return True
        return False


# --- items and levels --------------------------------------------------------

class Item:
    """One slice. It runs `action`, opens `children` (a function returning a Level), or holds a `dial`.
    A slice may have both an action and children: letting go on it runs the action, pushing through
    it (or dwelling) opens the children (e.g. a mode: switch to it, or open its tools)."""

    def __init__(self, id, label, icon=None, action=None, children=None, dial=None, active=False, danger=False,
                 swatch=None, repeat=True, push=True, enabled=True):
        self.id, self.label, self.icon = id, label, icon or id
        self.action, self.children, self.dial = action, children, dial
        self.active, self.danger, self.swatch = active, danger, swatch
        self.repeat = repeat and not danger and dial is None  # A tap repeats the last repeatable action.
        self.push = push  # running it is an undo step (not for Undo / Redo themselves)
        # A disabled slice keeps its place (so every slice keeps its direction) but does nothing.
        self.enabled = enabled

    @property
    def kind(self):
        if self.dial is not None:
            return 'dial'
        return 'branch' if self.children is not None else 'action'

    def text(self):
        """Label, with a dial's current value."""
        return f"{self.label} {self.dial.text()}" if self.dial is not None and self.enabled else self.label


class Level:
    """The slices of one pie. center: an optional Item with a dial, turned while the hand stays in the
    middle. More than MAX_SLICES items are paged: the last slice opens the rest."""

    def __init__(self, title, items, center=None, empty="", color=None):
        self.title, self.center, self.empty, self.color = title, center, empty, color
        items = [i for i in items if i is not None]
        if len(items) > MAX_SLICES:
            rest = items[MAX_SLICES - 1:]
            items = items[:MAX_SLICES - 1] + [
                Item("more_" + title, "More", "more",
                     children=lambda: Level(title, rest, color=color))]
        self.items = items


def walk(level, depth=6):
    """Every item reachable from level (opening branches), depth first."""
    for item in level.items:
        yield item
        if item.children is not None and depth > 0:
            yield from walk(item.children(), depth - 1)
    if level.center is not None:
        yield level.center


def find(level, id, depth=6):
    """The first item with this id under level, or None."""
    return next((i for i in walk(level, depth) if i.id == id), None)


def path_to(level, id, depth=6):
    """Slice indices to reach item id from level (branches opened along the way), or None."""
    for i, item in enumerate(level.items):
        if item.id == id:
            return [i]
        if item.children is not None and depth > 0:
            sub = path_to(item.children(), id, depth - 1)
            if sub is not None:
                return [i] + sub
    return None


# --- the pie ------------------------------------------------------------------

class Frame:
    def __init__(self, level, center, via=None):
        self.level, self.center, self.via = level, center, via  # via: the parent's slice index


class Pie:
    """An open pie menu. kind: 'MODE', 'TOOL', 'OBJECT' or 'TIME'. matrix: world matrix of the pie's
    plane (facing the viewer, scaled by the viewer scale), centered where the hand was."""

    def __init__(self, kind, hand, level, matrix, rot, now=None):
        now = time.monotonic() if now is None else now
        self.kind, self.hand = kind, hand
        self.matrix = matrix
        self.inv = matrix.inverted_safe()
        self.frames = [Frame(level, Vector((0.0, 0.0)))]
        self.hover = CENTER if level.center is not None else None
        self.hover_since = now
        self.by_stick = False
        self.stick_armed = True
        self.stick_anchor = None
        self.pointer = Vector((0.0, 0.0))  # where the hand (or stick) points, relative to the pie's center
        self.hand_point = Vector((0.0, 0.0))  # the hand, in pie space
        self.prev_rot = rot.copy()
        self.twist = 0.0  # since the hover last changed
        self.grabbed = None  # the Item whose dial is being turned
        self.grab_notch = 0
        self.sticky = False
        self.closed = False
        self.used = False
        self.t0 = self.last_change = now
        self.ran = None  # the Item whose action ran, for the caller (undo, repeat)
        self.changed = False

    @property
    def level(self):
        return self.frames[-1].level

    @property
    def center(self):
        return self.frames[-1].center

    def local(self, pos):
        p = self.inv @ Vector(pos)
        return Vector((p.x, p.y))

    def hovered(self):
        if self.hover == CENTER:
            return self.level.center
        if self.hover is None or self.hover >= len(self.level.items):
            return None
        return self.level.items[self.hover]

    def world_center(self):
        c = self.center
        return self.matrix @ Vector((c.x, c.y, 0.0))

    # update -------------------------------------------------------------

    def update(self, pos, rot, stick=(0.0, 0.0), now=None, dt=1.0 / 90.0):
        """Follow the hand and stick. Returns events for haptics: 'hover', 'open', 'back', 'grab', 'tick'."""
        now = time.monotonic() if now is None else now
        events = []
        delta = twist(self.prev_rot, rot)
        self.prev_rot = rot.copy()
        p = self.local(pos)
        self.hand_point = p.copy()
        stick = Vector(stick)

        if self.sticky and ((p - self.center).length > CLOSE_DISTANCE or now - self.last_change > STICKY_TIMEOUT):
            self.closed = True
            return ['close']

        if self.grabbed is not None:
            angle = self.twist + delta + stick.x * STICK_RATE * dt * (1.0 if abs(stick.x) > STICK_REARM else 0.0)
            self.twist = angle
            if self.grabbed.dial.turn(angle):
                self.changed = True
            n = self.grabbed.dial.notch(angle)
            if n != self.grab_notch:
                self.grab_notch = n
                events.append('tick')
            return events

        # Back up a level: the hand returns to where the parent pie was.
        if len(self.frames) > 1:
            parent = self.frames[-2].center
            if (self.center - parent).length > 2 * BACK and (p - parent).length < BACK:
                self.frames.pop()
                self._set_hover(None, now, events, quiet=True)
                self.stick_armed = False
                events.append('back')
                return events

        rel = p - self.center
        n = len(self.level.items)
        if stick.length < STICK_REARM:
            self.stick_armed = True
        hover = None
        if stick.length > STICK_PICK and self.stick_armed and n:
            hover = slice_index(stick.x, stick.y, n)
            self.by_stick = True
            self.stick_anchor = p.copy()
            self.pointer = stick.normalized() * RING_INNER
        elif self.by_stick and self.stick_anchor is not None and (p - self.stick_anchor).length < DEAD:
            hover = self.hover  # The stick's pick holds until the hand moves.
        else:
            self.by_stick = False
            self.pointer = rel.copy()
            if rel.length > DEAD and n:
                hover = slice_index(rel.x, rel.y, n)
        if hover is None and self.level.center is not None:
            hover = CENTER
        self._set_hover(hover, now, events)

        item = self.hovered()
        if item is None or not item.enabled:
            return events
        if item.children is not None and item.dial is None:
            pushed = not self.by_stick and rel.length > PUSH
            waited = now - self.hover_since > (STICK_DWELL if self.by_stick else DWELL)
            if pushed or waited:
                self.open(item, p, now)
                events.append('open')
            return events
        if item.dial is not None:
            self.twist += delta
            if abs(self.twist) > TWIST_GRAB:
                self.grabbed = item
                self.used = True
                item.dial.begin()
                if item.dial.turn(self.twist):
                    self.changed = True
                self.grab_notch = item.dial.notch(self.twist)
                self.last_change = now
                events.append('grab')
        return events

    def _set_hover(self, hover, now, events, quiet=False):
        if hover == self.hover:
            return
        self.hover = hover
        self.hover_since = self.last_change = now
        self.twist = 0.0
        if hover is not None and hover != CENTER:
            self.used = True
            if not quiet:
                events.append('hover')

    def open(self, item, at=None, now=None):
        """Show item's children as the current pie, centered at `at` (pie space; default: here)."""
        now = time.monotonic() if now is None else now
        index = self.hover
        level = item.children()
        self.frames.append(Frame(level, at.copy() if at is not None else self.center.copy(), index))
        self.used = True
        self.stick_armed = False
        self.by_stick = False
        self.hover = None
        self._set_hover(CENTER if level.center is not None else None, now, [], quiet=True)
        self.last_change = now

    # ending ---------------------------------------------------------------

    def release(self, now=None):
        """The button was let go. Returns 'tap', 'done', 'sticky' or 'cancel'."""
        now = time.monotonic() if now is None else now
        if self.sticky:
            return 'sticky'  # The release of the press that made it sticky, or a later one.
        if self.grabbed is not None:
            return self._end('done')
        item = self.hovered()
        if item is not None and item is not self.level.center:
            return self._choose(item, now=now)
        if not self.used and now - self.t0 < TAP:
            return self._end('tap')
        return self._end('cancel')

    def confirm(self, now=None):
        """A trigger press (or the same button again) while the pie is open: the same as letting go,
        but in the middle it closes the pie. Returns 'done', 'sticky' (a branch opened) or 'cancel'."""
        if self.grabbed is not None:
            return self._end('done')
        item = self.hovered()
        if item is None or item is self.level.center:
            return self._end('cancel')
        return self._choose(item, now=now)

    def _choose(self, item, now=None):
        """Run item's action; a branch without one opens and stays open (sticky)."""
        if not item.enabled:
            return self._end('cancel')
        if item.action is not None:
            item.action()
            self.ran = item
            self.changed = True
            return self._end('done')
        if item.children is not None:
            self.open(item, self.hand_point, now)
            self.sticky = True
            return 'sticky'
        return self._end('cancel')

    def _end(self, outcome):
        self.closed = True
        return outcome


class Knob:
    """A stick click held down: twisting that hand turns `dial`. A short press without a twist is a tap."""

    def __init__(self, hand, dial, rot, now=None):
        self.hand, self.dial = hand, dial
        self.prev_rot = rot.copy()
        self.angle = 0.0
        self.grabbed = False
        self.notch = 0
        self.changed = False
        self.t0 = time.monotonic() if now is None else now

    def update(self, rot):
        """Returns events for haptics: 'grab', 'tick'."""
        events = []
        self.angle += twist(self.prev_rot, rot)
        self.prev_rot = rot.copy()
        if self.dial is None:
            return events
        if not self.grabbed:
            if abs(self.angle) <= TWIST_GRAB:
                return events
            self.grabbed = True
            self.dial.begin()
            events.append('grab')
        if self.dial.turn(self.angle):
            self.changed = True
        n = self.dial.notch(self.angle)
        if n != self.notch:
            self.notch = n
            events.append('tick')
        return events

    def release(self, now=None):
        now = time.monotonic() if now is None else now
        return 'done' if self.grabbed else 'tap' if now - self.t0 < TAP * 2 else 'cancel'


# --- what is open --------------------------------------------------------------

pie = None  # the open Pie (one at a time)
knobs = {}  # hand index -> Knob
last_action = None  # the last repeatable Item that ran from a pie
_toast = {"text": "", "color": (1.0, 1.0, 1.0), "time": -100.0}
_hints = {"time": -100.0}

TOAST_SECONDS = 1.6
HINT_SECONDS = 7.0


def busy(hand_index):
    """Whether this hand's stick belongs to a pie or knob right now (so it doesn't navigate)."""
    return (pie is not None and not pie.closed and pie.hand == hand_index) or hand_index in knobs


def sticky_pie():
    return pie if pie is not None and not pie.closed and pie.sticky else None


def close():
    global pie
    pie = None


def remember(item):
    global last_action
    if item is not None and item.repeat and item.action is not None:
        last_action = item


def toast(text, color=(1.0, 1.0, 1.0)):
    """A short message by the right hand's status badge."""
    _toast.update(text=text, color=color, time=time.monotonic())


def current_toast():
    """(text, color, alpha) or None."""
    age = time.monotonic() - _toast["time"]
    if age > TOAST_SECONDS or not _toast["text"]:
        return None
    return _toast["text"], _toast["color"], min(1.0, (TOAST_SECONDS - age) / 0.4)


def show_hints(seconds=HINT_SECONDS):
    _hints.update(time=time.monotonic(), seconds=seconds)


def hints_alpha():
    seconds = _hints.get("seconds", HINT_SECONDS)
    age = time.monotonic() - _hints["time"]
    return 0.0 if age > seconds else min(1.0, (seconds - age) / 0.8)


def reset():
    global pie, last_action
    pie = None
    knobs.clear()
    last_action = None
