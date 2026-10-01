/**
 * RollingOdometer - zero-dependency mechanical rolling odometer.
 * Smooth vertical rolling digits with staggered delays and a fixed easing.
 *
 * The dashboard's meter renders a fixed
 * number of integer digits (leading zeros flagged `--lead`), fraction digits
 * flagged `--fraction`, the first digit of each thousands group flagged
 * `--group-start`, and no separators other than the decimal point.
 *
 * The register has two physical forms, chosen from the effective theme:
 *  - light: mechanical drums (`.odometer-digit` > `.odometer-ribbon`).
 *  - dark:  Nixie tubes (`.nixie-tube`, `.register--nixie` on the container).
 * Switching theme converts the form instantly and keeps the value. Styling
 * lives in dashboard.css (drums) and register.css (tubes, boot, celebration).
 *
 * `boot(value)` plays the once-per-page-load sequence: drums spin like a slot
 * machine, tubes cycle their cathodes, and both settle right to left.
 */
(function (global) {
  'use strict';

  const ROLL_EASING = 'cubic-bezier(0.2, 0.7, 0.1, 1)';
  const STAGGER_MS = 35;

  // Nixie value change: old numeral fades 120ms, new fades 80ms starting 40ms in,
  // then a brightness surge. Changed tubes are staggered right to left.
  const NIXIE_STAGGER_MS = 28;
  const NIXIE_FADE_IN_DELAY_MS = 40;
  const NIXIE_SURGE_DELAY_MS = 120;
  const NIXIE_SURGE_MS = 260;
  const NIXIE_SURGE_PEAK = 1.35;

  // Boot sequence timings.
  const BOOT_CYCLE_MS = 50;
  const BOOT_FIRST_LOCK_MS = 700;
  const BOOT_LOCK_STEP_MS = 110;
  const BOOT_LOCK_SPAN_MS = 560; // last tube locks within first lock + this
  const BOOT_DRUM_FIRST_MS = 800;
  const BOOT_DRUM_SPAN_MS = 650;
  const BOOT_DRUM_EASING = 'cubic-bezier(.12, .6, .1, 1)';
  const BOOT_DRUM_REPEATS = 3; // ribbon length during the spin, in 0-9 sequences
  const CLUNK_MS = 120;

  // Cathode numerals: hand-built strokes in a 28x48 box, modelled on IN-14
  // style Nixie digits (thin wire, round caps, elongated oval 0, flagged 1,
  // open-top 4, curly 2). Stroke styling lives in register.css.
  const NIXIE_PATHS = [
    // 0
    'M14 4C21 4 24 12 24 24S21 44 14 44S4 36 4 24S7 4 14 4Z',
    // 1
    'M7 14.5C10.5 12.5 13.5 9 15 5V43',
    // 2
    'M5 15C5 8.5 9 4 14 4S23 8.5 23 15C23 23 12 31 5 43.5H24',
    // 3
    'M5 8.5C8 5.5 11 4 14 4C19 4 23 7.5 23 12.5S19 22 12 22C19 22 24 26.5 24 33S19.5 44 13.5 44C9.5 44 6.5 42.5 4.5 39.5',
    // 4
    'M16 5L4 32H25M19.5 15V44',
    // 5
    'M23 5H8L6.5 21C9 19 11.5 18 14.5 18C20.5 18 24 23 24 30.5S19.5 44 13.5 44C9.5 44 6.5 42 4.5 39',
    // 6
    'M21 5.5C18 4 16 4 14 4C8 4 4 12 4 29C4 38.5 8.5 44 14 44C20 44 24 39.5 24 33C24 26.5 19.5 22 14 22C9 22 5.5 25 4 29.5',
    // 7
    'M4 5H24C19 18 14.5 31 11.5 44',
    // 8
    'M14 4C9 4 6 7.5 6 12.5S9.5 21 14 22C19 23 24 27 24 33.5S19.5 44 14 44S4 39.5 4 33.5S9 23 14 22C18.5 21 22 17.5 22 12.5S19 4 14 4Z',
    // 9
    'M7 42.5C10 44 12 44 14 44C20 44 24 36 24 19C24 9.5 19.5 4 14 4C8 4 4 8.5 4 15C4 21.5 8.5 26 14 26C19 26 22.5 23 24 18.5',
  ];

  let tubeMarkupCache = null;

  /** Inner markup shared by every tube: glass + bloom, cathode stack, lit numerals, socket. */
  function tubeMarkup() {
    if (tubeMarkupCache) return tubeMarkupCache;
    const off = NIXIE_PATHS.map((d) => `<path d="${d}"/>`).join('');
    const lit = NIXIE_PATHS
      .map((d) => `<g class="nixie__num"><path class="nixie__body" d="${d}"/><path class="nixie__wire" d="${d}"/></g>`)
      .join('');
    tubeMarkupCache = '<span class="nixie__glass"><span class="nixie__bloom"></span></span>'
      + `<svg class="nixie__stack" viewBox="0 0 28 48" focusable="false">${off}</svg>`
      + `<span class="nixie__lit"><svg viewBox="0 0 28 48" focusable="false">${lit}</svg></span>`
      + '<span class="nixie__socket"></span>';
    return tubeMarkupCache;
  }

  function prefersReducedMotion() {
    return typeof global.matchMedia === 'function'
      && global.matchMedia('(prefers-reduced-motion: reduce)').matches;
  }

  /** Effective dark theme: data-theme="dark", or no data-theme="light" while the OS is dark. */
  function effectiveDark() {
    const forced = global.document.documentElement.getAttribute('data-theme');
    if (forced === 'dark') return true;
    if (forced === 'light') return false;
    return typeof global.matchMedia === 'function'
      && global.matchMedia('(prefers-color-scheme: dark)').matches;
  }

  function now() {
    return global.performance && typeof global.performance.now === 'function'
      ? global.performance.now()
      : Date.now();
  }

  class RollingOdometer {
    /**
     * @param {Object} options
     * @param {HTMLElement|string} options.element - Target DOM element or CSS selector
     * @param {string} [options.prefix=""] - Prefix string (e.g. "$"); only announced, not drawn
     * @param {number} [options.decimals=0] - Decimal places (e.g. 2)
     * @param {number} [options.duration=800] - Roll animation duration in ms
     * @param {number} [options.minIntegerDigits=0] - Minimum integer digits, zero padded
     * @param {boolean} [options.nixie=true] - Render Nixie tubes in the dark theme
     */
    constructor(options = {}) {
      if (!options.element) {
        throw new Error('RollingOdometer: "element" option is required.');
      }

      this.el = typeof options.element === 'string'
        ? document.querySelector(options.element)
        : options.element;

      if (!this.el) {
        throw new Error(`RollingOdometer: Element not found: ${options.element}`);
      }

      this.prefix = options.prefix || '';
      this.decimals = typeof options.decimals === 'number' ? options.decimals : 0;
      this.duration = options.duration || 800;
      this.allowNixie = options.nixie !== false;
      this.minIntegerDigits = Math.max(0, Number(options.minIntegerDigits) || 0);

      this.currentValue = 0;
      this.slots = []; // Mounted slot descriptors: { type: 'digit'|'sep', char, element, ribbon, ... }
      this._nixie = false;
      this._boot = null;

      this._init();
    }

    _init() {
      // The drums are hidden from assistive technology: every ribbon holds all
      // ten digits, so exposing them would read as a long run of digits. The
      // container exposes one stable, current value instead.
      this.el.setAttribute('role', 'img');

      this._nixie = this._wantNixie();
      this.el.classList.toggle('register--nixie', this._nixie);
      if (this.allowNixie) this._watchTheme();

      this.update(0, true);
    }

    _wantNixie() {
      return this.allowNixie && effectiveDark();
    }

    _watchTheme() {
      this._onThemeChange = () => this._syncMode();
      document.addEventListener('themechange', this._onThemeChange);
      global.addEventListener('themechange', this._onThemeChange);
      if (typeof global.matchMedia === 'function') {
        this._mql = global.matchMedia('(prefers-color-scheme: dark)');
        if (typeof this._mql.addEventListener === 'function') {
          this._mql.addEventListener('change', this._onThemeChange);
        } else if (typeof this._mql.addListener === 'function') {
          this._mql.addListener(this._onThemeChange);
        }
      }
    }

    /** Convert between drums and tubes instantly, keeping the value. */
    _syncMode() {
      const next = this._wantNixie();
      if (next === this._nixie) return;
      this._cancelBoot();
      this._nixie = next;
      this.el.classList.toggle('register--nixie', next);
      this.slots = []; // forces a rebuild in the new form
      this.update(this.currentValue, true);
    }

    /**
     * Round once for both the drums and the accessible value.
     */
    _formatValue(num) {
      const digits = this.decimals > 0 ? this.decimals : 0;
      return num.toLocaleString('en-US', {
        useGrouping: false,
        minimumFractionDigits: digits,
        maximumFractionDigits: digits,
      });
    }

    /**
     * Break the already-rounded value into slot descriptors.
     * @param {string} formatted
     * @returns {Array<{char: string, type: 'digit'|'sep', lead?: boolean, fraction?: boolean, groupStart?: boolean, point?: boolean}>}
     */
    _describe(formatted) {
      const negative = formatted.startsWith('-');
      const parts = (negative ? formatted.slice(1) : formatted).split('.');
      let intPart = parts[0];
      const decPart = parts[1] || '';
      if (this.minIntegerDigits > intPart.length) {
        intPart = intPart.padStart(this.minIntegerDigits, '0');
      }

      const descs = [];
      if (negative) descs.push({ char: '-', type: 'sep' });

      const intLen = intPart.length;
      let leading = true;
      intPart.split('').forEach((char, i) => {
        if (char !== '0') leading = false;
        const fromEnd = intLen - i;
        const atGroupBoundary = i > 0 && fromEnd % 3 === 0;
        descs.push({
          char,
          type: 'digit',
          // Padding zeros before the first significant digit; the units digit always reads as live.
          lead: leading && char === '0' && i < intLen - 1,
          groupStart: atGroupBoundary,
        });
      });

      if (decPart) {
        descs.push({ char: '.', type: 'sep', point: true });
        decPart.split('').forEach((char) => descs.push({ char, type: 'digit', fraction: true }));
      }
      return descs;
    }

    _ariaValue(formatted) {
      const [integer, fraction] = formatted.split('.');
      const grouped = integer.replace(/\B(?=(\d{3})+(?!\d))/g, ',');
      return `${this.prefix}${grouped}${fraction === undefined ? '' : `.${fraction}`}`;
    }

    /**
     * Update the odometer display with a new value
     * @param {number|string} newValue
     * @param {boolean} [forceImmediate=false]
     */
    update(newValue, forceImmediate = false) {
      const parsed = Number(newValue);
      const num = Number.isFinite(parsed) ? parsed : 0;
      const formatted = this._formatValue(num);

      let forced = forceImmediate;
      if (this._boot) {
        if (formatted === this._boot.target) {
          // Same reading as the sequence is already settling on.
          this.currentValue = num;
          return;
        }
        // A different value mid-boot: abandon the sequence and land cleanly.
        this._cancelBoot();
        forced = true;
      }

      const isInitial = this.slots.length === 0;
      const immediate = forced || prefersReducedMotion();
      this.currentValue = num;

      const descs = this._describe(formatted);
      this.el.setAttribute('aria-label', this._ariaValue(formatted));

      const structureMatches = !isInitial &&
        this.slots.length === descs.length &&
        this.slots.every((slot, idx) => {
          const desc = descs[idx];
          if (desc.type !== slot.type) return false;
          if (desc.type === 'sep') return slot.char === desc.char;
          return !!slot.nixie === this._nixie &&
            !!slot.fraction === !!desc.fraction &&
            !!slot.groupStart === !!desc.groupStart;
        });

      if (structureMatches) {
        if (this._nixie) this._updateTubes(descs, immediate);
        else this._animateExistingSlots(descs, immediate);
      } else {
        this._rebuildSlots(descs, immediate);
      }
    }

    /* ------------------------------------------------------------- drums */

    _rollTo(slot, delay, immediate) {
      if (immediate) {
        slot.ribbon.style.transition = 'none';
      } else {
        slot.ribbon.style.transition = `transform ${this.duration}ms ${ROLL_EASING} ${delay}ms`;
      }
      slot.ribbon.style.transform = `translateY(-${slot.currentDigit * 10}%)`;
    }

    _animateExistingSlots(descs, immediate) {
      const totalDigits = this.slots.filter((s) => s.type === 'digit').length;
      let digitIdx = 0;

      this.slots.forEach((slot, idx) => {
        if (slot.type !== 'digit') return;
        const desc = descs[idx];
        slot.currentDigit = parseInt(desc.char, 10);
        slot.element.setAttribute('data-digit', slot.currentDigit);
        slot.element.classList.toggle('odometer-digit--lead', !!desc.lead);
        this._rollTo(slot, (totalDigits - 1 - digitIdx) * STAGGER_MS, immediate);
        digitIdx++;
      });
    }

    /* ------------------------------------------------------------- tubes */

    _buildTube(desc) {
      const digit = parseInt(desc.char, 10);
      const tube = document.createElement('span');
      tube.className = 'nixie-tube';
      if (desc.lead) tube.classList.add('nixie-tube--blank');
      if (desc.fraction) tube.classList.add('nixie-tube--fraction');
      if (desc.groupStart) tube.classList.add('nixie-tube--group-start');
      tube.setAttribute('aria-hidden', 'true');
      tube.setAttribute('data-digit', digit);
      tube.innerHTML = tubeMarkup();

      const nums = Array.prototype.slice.call(tube.querySelectorAll('.nixie__num'));
      if (!desc.lead) nums[digit].classList.add('is-on');

      return {
        type: 'digit',
        nixie: true,
        char: desc.char,
        currentDigit: digit,
        lead: !!desc.lead,
        fraction: !!desc.fraction,
        groupStart: !!desc.groupStart,
        element: tube,
        nums,
        bloom: tube.querySelector('.nixie__bloom'),
        lit: tube.querySelector('.nixie__lit'),
        surge: null,
      };
    }

    /** Light exactly one cathode (or none for a blank tube). No transitions are scheduled here. */
    _setTube(slot, digit, lead) {
      slot.nums.forEach((num, i) => num.classList.toggle('is-on', !lead && i === digit));
      slot.element.classList.toggle('nixie-tube--blank', lead);
      slot.element.setAttribute('data-digit', digit);
      slot.currentDigit = digit;
      slot.lead = lead;
    }

    /** Run `fn` with the tubes' fades suppressed, then restore them without animating. */
    _snap(fn) {
      this.el.classList.add('is-snap');
      fn();
      void this.el.offsetWidth; // commit the new state while transitions are off
      this.el.classList.remove('is-snap');
    }

    /** Brightness surge from `peak` back to 1 on a tube's lit layer. */
    _surge(slot, delay, peak = NIXIE_SURGE_PEAK, duration = NIXIE_SURGE_MS) {
      if (typeof slot.lit.animate !== 'function') return null;
      if (slot.surge) slot.surge.cancel();
      const anim = slot.lit.animate(
        [{ filter: `brightness(${peak})` }, { filter: 'brightness(1)' }],
        { duration, delay, easing: 'cubic-bezier(.2, .6, .3, 1)' },
      );
      anim.onfinish = () => { if (slot.surge === anim) slot.surge = null; };
      slot.surge = anim;
      if (this._boot) this._boot.anims.push(anim);
      return anim;
    }

    /** Fade one tube to a new numeral (or blank) after `delay`, then surge. */
    _fadeTube(slot, digit, lead, delay) {
      const wasLead = slot.lead;
      if (!wasLead) slot.nums[slot.currentDigit].style.transitionDelay = `${delay}ms`;
      if (!lead) slot.nums[digit].style.transitionDelay = `${delay + NIXIE_FADE_IN_DELAY_MS}ms`;
      slot.bloom.style.transitionDelay = `${lead ? delay : delay + NIXIE_FADE_IN_DELAY_MS}ms`;
      this._setTube(slot, digit, lead);
      if (!lead) this._surge(slot, delay + NIXIE_SURGE_DELAY_MS);
    }

    _updateTubes(descs, immediate) {
      const changed = [];
      this.slots.forEach((slot, idx) => {
        if (slot.type !== 'digit') return;
        const desc = descs[idx];
        const digit = parseInt(desc.char, 10);
        const lead = !!desc.lead;
        if (slot.currentDigit !== digit || slot.lead !== lead) changed.push({ slot, digit, lead });
      });
      changed.reverse(); // right to left

      if (immediate) {
        this._snap(() => changed.forEach((c) => this._setTube(c.slot, c.digit, c.lead)));
        return;
      }
      changed.forEach((c, k) => this._fadeTube(c.slot, c.digit, c.lead, k * NIXIE_STAGGER_MS));
    }

    /* -------------------------------------------------------------- build */

    _rebuildSlots(descs, immediate) {
      this.el.innerHTML = '';
      this.slots = [];

      const digitSlots = [];
      const newSlots = [];

      descs.forEach((desc) => {
        if (desc.type === 'digit') {
          if (this._nixie) {
            const tubeSlot = this._buildTube(desc);
            this.el.appendChild(tubeSlot.element);
            newSlots.push(tubeSlot);
            digitSlots.push(tubeSlot);
            return;
          }

          const digit = parseInt(desc.char, 10);
          const digitEl = document.createElement('span');
          digitEl.className = 'odometer-digit';
          if (desc.lead) digitEl.classList.add('odometer-digit--lead');
          if (desc.fraction) digitEl.classList.add('odometer-digit--fraction');
          if (desc.groupStart) digitEl.classList.add('odometer-digit--group-start');
          digitEl.setAttribute('aria-hidden', 'true');
          digitEl.setAttribute('data-digit', digit);

          const ribbon = document.createElement('span');
          ribbon.className = 'odometer-ribbon';
          ribbon.setAttribute('aria-hidden', 'true');
          for (let i = 0; i <= 9; i++) {
            ribbon.appendChild(this._numSpan(i));
          }

          // Start every drum at 0 so a fresh register rolls up to its value.
          ribbon.style.transform = 'translateY(0%)';
          digitEl.appendChild(ribbon);
          this.el.appendChild(digitEl);

          const slot = {
            type: 'digit',
            char: desc.char,
            currentDigit: digit,
            fraction: !!desc.fraction,
            groupStart: !!desc.groupStart,
            element: digitEl,
            ribbon,
          };
          newSlots.push(slot);
          digitSlots.push(slot);
        } else {
          const sepEl = document.createElement('span');
          sepEl.className = 'odometer-separator';
          sepEl.setAttribute('aria-hidden', 'true');
          if (desc.point) {
            if (this._nixie) {
              sepEl.classList.add('nixie-point');
              sepEl.innerHTML = '<span class="nixie-point__dot"></span>';
            } else {
              sepEl.classList.add('odometer-separator--point');
            }
          } else {
            sepEl.textContent = desc.char;
          }
          this.el.appendChild(sepEl);
          newSlots.push({ type: 'sep', char: desc.char, element: sepEl });
        }
      });

      this.slots = newSlots;

      const totalDigits = digitSlots.length;
      if (this._nixie) {
        // Tubes are built already showing their value; a live rebuild just surges them.
        if (!immediate) {
          digitSlots.slice().reverse().forEach((slot, k) => {
            if (!slot.lead) this._surge(slot, k * NIXIE_STAGGER_MS);
          });
        }
        return;
      }

      if (immediate) {
        digitSlots.forEach((slot) => this._rollTo(slot, 0, true));
      } else {
        requestAnimationFrame(() => {
          requestAnimationFrame(() => {
            digitSlots.forEach((slot, idx) => {
              this._rollTo(slot, (totalDigits - 1 - idx) * STAGGER_MS, false);
            });
          });
        });
      }
    }

    _numSpan(i) {
      const numSpan = document.createElement('span');
      numSpan.className = 'odometer-num';
      numSpan.textContent = String(i);
      return numSpan;
    }

    /* --------------------------------------------------------------- boot */

    /**
     * The once-per-page-load sequence. The aria-label already reads the final
     * value; reduced motion shows it instantly.
     * @param {number|string} value
     */
    boot(value) {
      this._cancelBoot();
      this.update(value, true);
      if (prefersReducedMotion()) return;
      if (!this.slots.some((s) => s.type === 'digit')) return;

      const target = this._formatValue(this.currentValue);
      this._boot = { target, raf: 0, anims: [], cleanup: null };
      if (this._nixie) this._bootTubes();
      else this._bootDrums();
    }

    _cancelBoot() {
      const boot = this._boot;
      if (!boot) return;
      this._boot = null;
      if (boot.raf) global.cancelAnimationFrame(boot.raf);
      if (typeof boot.cleanup === 'function') boot.cleanup();
      boot.anims.forEach((anim) => { try { anim.cancel(); } catch (err) { /* already gone */ } });
    }

    _bootTubes() {
      const boot = this._boot;
      const slots = this.slots.filter((s) => s.type === 'digit');
      const n = slots.length;
      const step = Math.min(BOOT_LOCK_STEP_MS, BOOT_LOCK_SPAN_MS / Math.max(1, n - 1));
      const final = slots.map((slot) => ({ digit: slot.currentDigit, lead: slot.lead }));

      // Right to left: the last cents tube locks first.
      slots.forEach((slot, i) => {
        slot.lockAt = BOOT_FIRST_LOCK_MS + (n - 1 - i) * step;
        slot.locked = false;
        slot.cycleStart = (i * 3 + (i % 2) * 5 + 2) % 10;
        slot.cyclePhase = (i * 17 + 7) % BOOT_CYCLE_MS;
        slot.cycleDigit = -1;
        slot.element.classList.add('is-cycling');
      });

      const showCycle = (slot, t) => {
        const d = (Math.floor((t + slot.cyclePhase) / BOOT_CYCLE_MS) + slot.cycleStart) % 10;
        if (d === slot.cycleDigit) return;
        slot.cycleDigit = d;
        this._setTube(slot, d, false);
      };

      boot.cleanup = () => {
        // Land every tube on its final reading, without fades.
        this._snap(() => slots.forEach((slot, i) => {
          slot.element.classList.remove('is-cycling');
          this._setTube(slot, final[i].digit, final[i].lead);
        }));
      };

      const t0 = now();
      this._snap(() => slots.forEach((slot) => showCycle(slot, 0)));

      const frame = () => {
        if (this._boot !== boot) return;
        const t = now() - t0;
        let pending = 0;
        slots.forEach((slot, i) => {
          if (slot.locked) return;
          if (t >= slot.lockAt) {
            slot.locked = true;
            if (final[i].lead) {
              // Fade to blank: let the normal transitions run.
              slot.nums.forEach((num) => { num.style.transitionDelay = ''; });
              slot.bloom.style.transitionDelay = '';
              slot.element.classList.remove('is-cycling');
              this._setTube(slot, final[i].digit, true);
            } else {
              // Lock hard on the final numeral, then surge.
              this._setTube(slot, final[i].digit, false);
              void slot.element.offsetWidth;
              slot.element.classList.remove('is-cycling');
              this._surge(slot, 0);
            }
          } else {
            showCycle(slot, t);
            pending++;
          }
        });
        if (pending > 0) {
          boot.raf = global.requestAnimationFrame(frame);
        } else {
          boot.raf = 0;
          // Surges finish on their own; the sequence is over.
          this._boot = null;
        }
      };
      boot.raf = global.requestAnimationFrame(frame);
    }

    _bootDrums() {
      const boot = this._boot;
      const slots = this.slots.filter((s) => s.type === 'digit');
      const n = slots.length;
      const step = Math.min(BOOT_LOCK_STEP_MS, BOOT_DRUM_SPAN_MS / Math.max(1, n - 1));
      const total = BOOT_DRUM_REPEATS * 10;
      let remaining = n;

      const collapse = (slot) => {
        // The spin ribbon repeats 0-9, so dropping the extra sequences is invisible.
        const ribbon = slot.ribbon;
        while (ribbon.children.length > 10) ribbon.removeChild(ribbon.lastChild);
        ribbon.style.transition = 'none';
        ribbon.style.transform = `translateY(-${slot.currentDigit * 10}%)`;
        ribbon.style.willChange = '';
      };

      boot.cleanup = () => slots.forEach(collapse);

      slots.forEach((slot, i) => {
        const ribbon = slot.ribbon;
        const settleAt = BOOT_DRUM_FIRST_MS + (n - 1 - i) * step;
        const digit = slot.currentDigit;

        for (let k = 10; k < total; k++) ribbon.appendChild(this._numSpan(k % 10));
        ribbon.style.transition = 'none';
        ribbon.style.transform = 'translateY(0%)';

        const endY = -((total - 10 + digit) * 100) / total;
        const blur = '.032em';
        const spin = ribbon.animate(
          [
            { transform: 'translateY(0%)', filter: `blur(${blur})` },
            { transform: `translateY(${endY}%)`, filter: 'blur(0)' },
          ],
          { duration: settleAt, easing: BOOT_DRUM_EASING, fill: 'forwards' },
        );
        boot.anims.push(spin);

        spin.onfinish = () => {
          if (this._boot !== boot) return;
          collapse(slot);
          spin.cancel();
          // A tiny mechanical clunk: 1px past the stop, settling back.
          const rest = `-${digit * 10}%`;
          const clunk = ribbon.animate(
            [
              { transform: `translateY(calc(${rest} - 1px))` },
              { transform: `translateY(${rest})` },
            ],
            { duration: CLUNK_MS, easing: 'ease-out' },
          );
          boot.anims.push(clunk);
          remaining--;
          if (remaining === 0) this._boot = null;
        };
      });
    }
  }

  // Export globally
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = RollingOdometer;
  } else {
    global.RollingOdometer = RollingOdometer;
  }
})(typeof window !== 'undefined' ? window : this);
