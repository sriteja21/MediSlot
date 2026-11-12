(() => {
  const patientTimezone = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
  let patientProfilePromise;

  function escapeHtml(value) {
    return String(value ?? '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#039;');
  }

  function showToast(message, type = 'info') {
    const toast = document.getElementById('patientNotice');
    if (!toast) return;
    toast.className = `patient-toast patient-toast-${type}`;
    toast.textContent = message;
    toast.hidden = false;
    window.clearTimeout(showToast.timeoutId);
    showToast.timeoutId = window.setTimeout(() => { toast.hidden = true; }, 6000);
  }

  function showFieldErrors(errors) {
    if (!errors || typeof errors !== 'object') return;
    Object.entries(errors).forEach(([name, message]) => {
      const field = document.querySelector(`[name="${CSS.escape(name)}"]`);
      if (!field) return;
      field.setAttribute('aria-invalid', 'true');
      const error = document.getElementById(`${field.id || name}Error`);
      if (error) {
        error.textContent = message;
        error.hidden = false;
      }
    });
  }

  function clearFieldErrors(form) {
    form?.querySelectorAll('[aria-invalid="true"]').forEach(field => field.removeAttribute('aria-invalid'));
    form?.querySelectorAll('.field-error').forEach(error => {
      error.textContent = '';
      error.hidden = true;
    });
  }

  function setBusy(button, busy, label = 'Saving...') {
    if (!button) return;
    if (busy) {
      button.dataset.originalLabel = button.textContent.trim();
      button.disabled = true;
      button.innerHTML = `<span class="spinner" aria-hidden="true"></span><span>${escapeHtml(label)}</span>`;
    } else {
      button.disabled = false;
      button.textContent = button.dataset.originalLabel || button.textContent;
      delete button.dataset.originalLabel;
    }
  }

  function setLoading(element, loading, message = 'Loading...') {
    if (!element) return;
    element.hidden = !loading;
    if (loading) element.innerHTML = `<span class="spinner" aria-hidden="true"></span><span>${escapeHtml(message)}</span>`;
  }

  async function api(url, options = {}) {
    let response;
    try {
      response = await fetch(url, { credentials: 'same-origin', ...options });
    } catch (error) {
      const unavailable = new Error('Could not reach MediSlot. Check your connection and retry.');
      unavailable.status = 0;
      throw unavailable;
    }

    let data = {};
    try {
      data = await response.json();
    } catch (error) {
      data = {};
    }

    if (response.status === 401) {
      const next = `${window.location.pathname}${window.location.search}`;
      window.location.assign(`/login?role=patient&expired=1&next=${encodeURIComponent(next)}`);
      const expired = new Error('Session expired, please log in again.');
      expired.status = 401;
      throw expired;
    }

    if (!response.ok) {
      const messages = {
        400: data.message || 'Check the highlighted fields and try again.',
        403: data.message || 'You do not have permission to do that.',
        404: data.message || 'The requested information was not found.',
        409: data.message || 'The request conflicts with the current state.',
      };
      const failure = new Error(messages[response.status] || data.message || 'MediSlot is temporarily unavailable. Please retry.');
      failure.status = response.status;
      failure.fieldErrors = data.field_errors || {};
      failure.correlationId = data.correlation_id || response.headers.get('X-Correlation-ID');
      throw failure;
    }
    return data;
  }

  async function loadPatientName() {
    const profile = await loadPatientProfile();
    const name = profile?.name || 'Patient';
    const firstName = name.trim().split(/\s+/)[0] || 'Patient';
    const element = document.getElementById('patientFirstName');
    if (element) element.textContent = firstName;
    return profile;
  }

  function loadPatientProfile() {
    if (!patientProfilePromise) {
      patientProfilePromise = api('/api/patient/me').then(data => data.profile);
    }
    return patientProfilePromise;
  }

  function setPatientProfile(profile) {
    patientProfilePromise = Promise.resolve(profile);
    const element = document.getElementById('patientFirstName');
    const firstName = String(profile?.name || 'Patient').trim().split(/\s+/)[0] || 'Patient';
    if (element) element.textContent = firstName;
  }

  function localDateTime(value, options = {}) {
    if (!value) return 'Time unavailable';
    const normalized = String(value).replace(' ', 'T');
    const hasZone = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(normalized);
    const parsed = new Date(hasZone ? normalized : `${normalized}Z`);
    if (Number.isNaN(parsed.getTime())) return String(value);
    return new Intl.DateTimeFormat(undefined, {
      timeZone: patientTimezone,
      dateStyle: options.dateStyle || 'medium',
      timeStyle: options.timeStyle || 'short',
      timeZoneName: 'short',
    }).format(parsed);
  }

  function localTime(value, timezone = patientTimezone) {
    const parsed = new Date(String(value).replace(' ', 'T'));
    if (Number.isNaN(parsed.getTime())) return 'Time unavailable';
    return new Intl.DateTimeFormat(undefined, {
      timeZone: timezone,
      hour: 'numeric',
      minute: '2-digit',
    }).format(parsed);
  }

  function timeZoneLabel(timezone = patientTimezone) {
    return new Intl.DateTimeFormat(undefined, { timeZone: timezone, timeZoneName: 'short' })
      .formatToParts(new Date()).find(part => part.type === 'timeZoneName')?.value || timezone;
  }

  function createTag(label) {
    const tag = document.createElement('span');
    tag.className = 'patient-tag';
    tag.textContent = String(label);
    return tag;
  }

  function setEmptyState(container, message, actionHtml = '') {
    if (!container) return;
    container.innerHTML = `<p>${escapeHtml(message)}</p>${actionHtml}`;
    container.hidden = false;
  }

  function setErrorState(container, message, retry) {
    if (!container) return;
    container.replaceChildren();
    const text = document.createElement('p');
    text.textContent = message;
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'btn btn-secondary btn-sm';
    button.textContent = 'Retry';
    button.addEventListener('click', retry);
    container.append(text, button);
    container.hidden = false;
  }

  function initializeNavigation() {
    const toggle = document.getElementById('patientMenuToggle');
    const nav = document.getElementById('patientNavigation');
    if (!toggle || !nav) return;
    toggle.addEventListener('click', () => {
      const expanded = toggle.getAttribute('aria-expanded') === 'true';
      toggle.setAttribute('aria-expanded', String(!expanded));
      toggle.setAttribute('aria-label', expanded ? 'Open navigation' : 'Close navigation');
      nav.classList.toggle('open', !expanded);
    });
    nav.addEventListener('click', event => {
      if (event.target.closest('a')) {
        toggle.setAttribute('aria-expanded', 'false');
        toggle.setAttribute('aria-label', 'Open navigation');
        nav.classList.remove('open');
      }
    });
    document.addEventListener('keydown', event => {
      if (event.key === 'Escape') {
        toggle.setAttribute('aria-expanded', 'false');
        toggle.setAttribute('aria-label', 'Open navigation');
        nav.classList.remove('open');
        toggle.focus();
      }
    });
  }

  document.addEventListener('DOMContentLoaded', () => {
    initializeNavigation();
    loadPatientName().catch(error => showToast(error.message, 'error'));
  });

  window.MediSlotPatient = {
    api,
    clearFieldErrors,
    createTag,
    escapeHtml,
    localDateTime,
    localTime,
    loadPatientProfile,
    patientTimezone,
    setPatientProfile,
    setBusy,
    setEmptyState,
    setErrorState,
    setLoading,
    showFieldErrors,
    showToast,
    timeZoneLabel,
  };
})();
