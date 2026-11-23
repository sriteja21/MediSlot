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
