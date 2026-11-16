// Protocol v2's question on the screen (enrol_confirm): someone typed this screen's code into a
// controller; the person in front of it allows that organisation to manage it, or not. One
// question at a time: a new one ends the one before, unanswered.
import { randomUUID } from "node:crypto";

export class Question {
  /** @param {() => void} changed called whenever the question shows or goes */
  constructor(changed = () => {}) {
    this.changed = changed;
    this.pending = null;
  }

  /**
   * Asks, and resolves true (allowed), false (not allowed) or null: nobody answered within
   * waitMs, or the controller stopped waiting (signal).
   */
  ask(organisation, host, { signal = null, waitMs = 290_000 } = {}) {
    this.pending?.finish(null);
    return new Promise((resolve) => {
      const question = { id: `v2-${randomUUID()}`, organisation, host, expires: Date.now() + waitMs };
      const timer = setTimeout(() => question.finish(null), waitMs);
      question.finish = (answer) => {
        if (this.pending !== question) return;
        clearTimeout(timer);
        this.pending = null;
        resolve(answer);
        this.changed();
      };
      if (signal?.aborted) return resolve(null);
      signal?.addEventListener("abort", () => question.finish(null), { once: true });
      this.pending = question;
      this.changed();
    });
  }

  /** The person's answer: false when nothing is asked (or not this, with an id). */
  answer(allow, id = null) {
    const question = this.pending;
    if (!question || (id !== null && id !== question.id)) return false;
    question.finish(Boolean(allow));
    return true;
  }
}
