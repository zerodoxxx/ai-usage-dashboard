/**
 * RollingOdometer - Zero-dependency mechanical rolling odometer visualizer.
 * Provides smooth vertical rolling digits with physical staggered delays and cubic-bezier easing.
 */
(function (global) {
  'use strict';

  class RollingOdometer {
    /**
     * @param {Object} options
     * @param {HTMLElement|string} options.element - Target DOM element or CSS selector
     * @param {string} [options.prefix=""] - Prefix string (e.g. "$")
     * @param {string} [options.suffix=""] - Suffix string (e.g. "%")
     * @param {number} [options.decimals=0] - Decimal places (e.g. 2 or 4)
     * @param {boolean} [options.formatCommas=true] - Format integer part with thousands separators
     * @param {number} [options.duration=800] - Roll animation duration in ms
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
      this.suffix = options.suffix || '';
      this.decimals = typeof options.decimals === 'number' ? options.decimals : 0;
      this.formatCommas = options.formatCommas !== undefined ? !!options.formatCommas : true;
      this.duration = options.duration || 800;

      this.currentValue = 0;
      this.slots = []; // Track mounted slot descriptors: { type: 'digit'|'sep', char, element, ribbon }

      this._init();
    }

    _init() {
      this.el.classList.add('odometer-container');
      this.update(0, true);
    }

    /**
     * Format a numeric value into an array of character descriptors
     * @param {number|string} val
     * @returns {string[]} array of individual characters
     */
    _formatToChars(val) {
      let num = Number(val);
      if (isNaN(num)) num = 0;

      const isNegative = num < 0;
      num = Math.abs(num);

      let formattedStr = '';
      if (this.decimals > 0) {
        const fixed = num.toFixed(this.decimals);
        const [intPart, decPart] = fixed.split('.');
        const withCommas = this.formatCommas
          ? intPart.replace(/\B(?=(\d{3})+(?!\d))/g, ',')
          : intPart;
        formattedStr = `${isNegative ? '-' : ''}${withCommas}.${decPart}`;
      } else {
        const rounded = Math.round(num);
        const withCommas = this.formatCommas
          ? String(rounded).replace(/\B(?=(\d{3})+(?!\d))/g, ',')
          : String(rounded);
        formattedStr = `${isNegative ? '-' : ''}${withCommas}`;
      }

      return formattedStr.split('');
    }

    /**
     * Update the odometer display with a new value
     * @param {number|string} newValue
     * @param {boolean} [forceImmediate=false]
     */
    update(newValue, forceImmediate = false) {
      const num = Number(newValue);
      const isInitial = this.slots.length === 0;
      this.currentValue = isNaN(num) ? 0 : num;

      const chars = this._formatToChars(this.currentValue);

      // Check if current slot structure matches the new chars pattern
      const structureMatches = !isInitial &&
        this.slots.length === chars.length &&
        this.slots.every((slot, idx) => {
          const isDigit = /\d/.test(chars[idx]);
          return isDigit ? slot.type === 'digit' : slot.type === 'sep' && slot.char === chars[idx];
        });

      if (structureMatches) {
        // Fast path: Reuse existing DOM ribbons, just animate to new digits
        this._animateExistingSlots(chars, forceImmediate);
      } else {
        // Rebuild slot structure
        this._rebuildSlots(chars, forceImmediate);
      }
    }

    _animateExistingSlots(chars, forceImmediate) {
      const digitSlots = this.slots.filter(s => s.type === 'digit');
      const totalDigits = digitSlots.length;
      let digitIdx = 0;

      this.slots.forEach((slot, idx) => {
        const char = chars[idx];
        if (slot.type === 'digit') {
          const digit = parseInt(char, 10);
          slot.currentDigit = digit;
          slot.element.setAttribute('data-digit', digit);

          const delay = forceImmediate ? 0 : (totalDigits - 1 - digitIdx) * 35;
          digitIdx++;

          if (forceImmediate) {
            slot.ribbon.style.transition = 'none';
            slot.ribbon.style.transform = `translateY(-${digit * 10}%)`;
          } else {
            slot.ribbon.style.transition = `transform ${this.duration}ms cubic-bezier(0.2, 0.9, 0.3, 1) ${delay}ms`;
            slot.ribbon.style.transform = `translateY(-${digit * 10}%)`;
          }
        }
      });
    }

    _rebuildSlots(chars, forceImmediate) {
      this.el.innerHTML = '';
      this.slots = [];

      // Render prefix if present
      if (this.prefix) {
        const prefixEl = document.createElement('span');
        prefixEl.className = 'odometer-prefix';
        prefixEl.textContent = this.prefix;
        this.el.appendChild(prefixEl);
      }

      // Build character slots
      const digitSlots = [];
      const newSlots = [];

      chars.forEach((char) => {
        const isDigit = /\d/.test(char);

        if (isDigit) {
          const digit = parseInt(char, 10);
          const digitEl = document.createElement('span');
          digitEl.className = 'odometer-digit';
          digitEl.setAttribute('data-digit', digit);

          const ribbon = document.createElement('span');
          ribbon.className = 'odometer-ribbon';

          // Stack spans 0 through 9
          for (let i = 0; i <= 9; i++) {
            const numSpan = document.createElement('span');
            numSpan.className = 'odometer-num';
            numSpan.textContent = String(i);
            ribbon.appendChild(numSpan);
          }

          // Initial position at 0
          ribbon.style.transform = 'translateY(0%)';
          digitEl.appendChild(ribbon);
          this.el.appendChild(digitEl);

          const slot = {
            type: 'digit',
            char,
            currentDigit: digit,
            element: digitEl,
            ribbon,
          };
          newSlots.push(slot);
          digitSlots.push(slot);
        } else {
          const sepEl = document.createElement('span');
          sepEl.className = 'odometer-separator';
          sepEl.textContent = char;
          this.el.appendChild(sepEl);

          newSlots.push({
            type: 'sep',
            char,
            element: sepEl,
          });
        }
      });

      // Render suffix if present
      if (this.suffix) {
        const suffixEl = document.createElement('span');
        suffixEl.className = 'odometer-suffix';
        suffixEl.textContent = this.suffix;
        this.el.appendChild(suffixEl);
      }

      this.slots = newSlots;

      // Trigger animation in next frame
      const totalDigits = digitSlots.length;
      if (forceImmediate) {
        digitSlots.forEach((slot) => {
          slot.ribbon.style.transition = 'none';
          slot.ribbon.style.transform = `translateY(-${slot.currentDigit * 10}%)`;
        });
      } else {
        requestAnimationFrame(() => {
          requestAnimationFrame(() => {
            digitSlots.forEach((slot, idx) => {
              const delay = (totalDigits - 1 - idx) * 35;
              slot.ribbon.style.transition = `transform ${this.duration}ms cubic-bezier(0.2, 0.9, 0.3, 1) ${delay}ms`;
              slot.ribbon.style.transform = `translateY(-${slot.currentDigit * 10}%)`;
            });
          });
        });
      }
    }

    /**
     * Return current numerical value
     * @returns {number}
     */
    getValue() {
      return this.currentValue;
    }

    /**
     * Clean up DOM
     */
    destroy() {
      this.el.innerHTML = '';
      this.el.classList.remove('odometer-container');
      this.slots = [];
    }
  }

  // Export globally
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = RollingOdometer;
  } else {
    global.RollingOdometer = RollingOdometer;
  }
})(typeof window !== 'undefined' ? window : this);
