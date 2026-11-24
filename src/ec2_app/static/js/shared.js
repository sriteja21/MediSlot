window.MediSlotTime = {
  normalize(value) {
    const text = String(value || '').replace(' ', 'T');
    return /(?:Z|[+-]\d{2}:?\d{2})$/i.test(text) ? text : `${text}Z`;
  },

  formatDateTime(value, timeZone) {
    if (!value) return 'Time unavailable';
    const date = new Date(this.normalize(value));
    if (Number.isNaN(date.getTime())) return String(value);
    return date.toLocaleString(undefined, timeZone ? { timeZone } : undefined);
  },

  formatTime(value, timeZone) {
    if (!value) return 'Time unavailable';
    const date = new Date(this.normalize(value));
    if (Number.isNaN(date.getTime())) return String(value);
    return date.toLocaleTimeString(undefined, {
      timeZone: timeZone || undefined,
      hour: 'numeric',
      minute: '2-digit',
    });
  },

  formatScheduleTime(value, timeZone) {
    if (!value) return '--:--';
    try {
      const date = new Date(this.normalize(value));
      if (Number.isNaN(date.getTime())) return String(value).substring(11, 16);
      return date.toLocaleTimeString([], {
        timeZone: timeZone || undefined,
        hour: '2-digit',
        minute: '2-digit',
        hour12: false,
      });
    } catch (error) {
      return String(value).substring(11, 16);
    }
  },

  formatAppointmentTime(value, timeZone) {
    if (!value) return 'TBD';
    const date = new Date(this.normalize(value));
    if (Number.isNaN(date.getTime())) return String(value);
    return date.toLocaleString(undefined, {
      timeZone: timeZone || 'UTC',
      year: 'numeric',
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    });
  },
};

window.MediSlotMoney = {
  round(value, fractionDigits = 2) {
    const amount = Number(value);
    const safeAmount = Number.isFinite(amount) ? amount : 0;
    return safeAmount.toFixed(fractionDigits);
  },

  formatINR(value, fractionDigits = 2) {
    return `₹${this.round(value, fractionDigits)}`;
  },
};

document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('.flash-container .alert').forEach(alert => {
    if (alert.classList.contains('alert-danger')) return;
    let dismissTimer;
    const dismiss = () => {
      alert.classList.add('is-dismissing');
      window.setTimeout(() => alert.remove(), 220);
    };
    const startTimer = () => {
      window.clearTimeout(dismissTimer);
      dismissTimer = window.setTimeout(dismiss, 6500);
    };
    const stopTimer = () => window.clearTimeout(dismissTimer);
    alert.addEventListener('mouseenter', stopTimer);
    alert.addEventListener('mouseleave', startTimer);
    alert.addEventListener('focusin', stopTimer);
    alert.addEventListener('focusout', startTimer);
    alert.querySelector('.alert-close')?.addEventListener('click', dismiss);
    startTimer();
  });

  document.querySelectorAll('.nav-item[aria-current="page"]').forEach(link => {
    link.setAttribute('aria-current', 'page');
  });
});
