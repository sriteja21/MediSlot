const DOCTORS_PER_PAGE = 8;
let doctorPage = 1;
let doctorTotal = 0;
let selectedDoctor = null;
let selectedConsultation = null;
let selectedSlot = null;

document.addEventListener('DOMContentLoaded', () => {
  const screen = document.querySelector('[data-patient-page]');
  if (!screen) return;

  const page = screen.dataset.patientPage;
  if (page === 'dashboard') loadDashboard();
  else loadPatientIdentity();

  if (page === 'doctors') initializeDoctorSearch();
  if (page === 'doctor-profile') initializeDoctorProfile(screen.dataset.doctorId);
  if (page === 'appointments') {
    document.getElementById('refreshAppointments').addEventListener('click', loadAppointments);
    loadAppointments();
  }
});

async function loadPatientIdentity() {
  try {
    const data = await fetchJson('/api/patient/dashboard');
    setPatientName(data.patient?.full_name);
  } catch (error) {
    showNotice(error.message || 'Could not load your profile.', 'danger');
  }
}

async function loadDashboard() {
  const loading = document.getElementById('dashboardAppointmentsLoading');
  const error = document.getElementById('dashboardAppointmentsError');
  const empty = document.getElementById('dashboardAppointmentsEmpty');
  const list = document.getElementById('dashboardAppointments');
  loading.hidden = false;
  error.hidden = true;
  empty.hidden = true;
  list.innerHTML = '';

  try {
    const data = await fetchJson('/api/patient/dashboard');
    setPatientName(data.patient?.full_name);
    document.getElementById('patientDashboardGreeting').textContent = data.patient?.full_name
      ? `Hello, ${data.patient.full_name}`
      : 'Patient dashboard';
    document.getElementById('upcomingCount').textContent = data.upcoming_appointments_count ?? 0;
    const appointments = data.appointments || [];
    const nextAppointment = appointments.find(item =>
      item.status === 'CONFIRMED' && new Date(normalizeDateTime(item.slot_start_utc)) >= new Date()
    );
    document.getElementById('nextAppointmentContent').innerHTML = nextAppointment
      ? `<strong>${escapeHtml(nextAppointment.doctor_name || 'Doctor')}</strong>
         <span>${escapeHtml(formatDateTime(nextAppointment.slot_start_utc))}</span>
         <span>${escapeHtml(nextAppointment.hospital_name || '')}</span>`
      : '<span>No upcoming visits</span><a class="patient-text-link" href="/patient/doctors">Find a doctor</a>';

    if (!appointments.length) {
      empty.hidden = false;
      return;
    }
    appointments.slice(0, 4).forEach(appointment => list.appendChild(createAppointmentItem(appointment)));
  } catch (errorValue) {
    error.textContent = errorValue.message || 'Could not load your dashboard.';
    error.hidden = false;
  } finally {
    loading.hidden = true;
  }
}

function initializeDoctorSearch() {
  document.getElementById('doctorSearchForm').addEventListener('submit', event => {
    event.preventDefault();
    doctorPage = 1;
    loadDoctors();
  });
  document.getElementById('previousDoctors').addEventListener('click', () => {
    if (doctorPage > 1) {
      doctorPage--;
      loadDoctors();
    }
  });
  document.getElementById('nextDoctors').addEventListener('click', () => {
    if (doctorPage * DOCTORS_PER_PAGE < doctorTotal) {
      doctorPage++;
      loadDoctors();
    }
  });
  loadDoctors();
}

async function loadDoctors() {
  const loading = document.getElementById('doctorSearchLoading');
  const results = document.getElementById('doctorResults');
  const empty = document.getElementById('doctorEmptyState');
  const error = document.getElementById('doctorSearchError');
  loading.hidden = false;
  error.hidden = true;
  empty.hidden = true;
  results.innerHTML = '';
  document.getElementById('doctorPagination').hidden = true;

  const params = new URLSearchParams({
    search: document.getElementById('doctorSearchInput').value.trim(),
    specialization: document.getElementById('specializationInput').value.trim(),
    hospital: document.getElementById('hospitalInput').value.trim(),
    page: String(doctorPage),
    limit: String(DOCTORS_PER_PAGE),
  });

  try {
    const data = await fetchJson(`/api/patient/doctors?${params}`);
    const doctors = data.doctors || [];
    doctorTotal = Number(data.total || 0);
    document.getElementById('doctorResultCount').textContent = doctorTotal
      ? `${doctorTotal} doctor${doctorTotal === 1 ? '' : 's'}`
      : 'No matching doctors';
    if (!doctors.length) {
      empty.hidden = false;
      return;
    }

    doctors.forEach(doctor => results.appendChild(createDoctorCard(doctor)));
    const pageCount = Math.max(1, Math.ceil(doctorTotal / DOCTORS_PER_PAGE));
    document.getElementById('doctorPageLabel').textContent = `Page ${doctorPage} of ${pageCount}`;
    document.getElementById('previousDoctors').disabled = doctorPage <= 1;
    document.getElementById('nextDoctors').disabled = doctorPage >= pageCount;
    document.getElementById('doctorPagination').hidden = pageCount <= 1;
  } catch (errorValue) {
    error.textContent = errorValue.message || 'Could not load doctors.';
    error.hidden = false;
  } finally {
    loading.hidden = true;
  }
}

function createDoctorCard(doctor) {
  const card = document.createElement('article');
  card.className = 'doctor-result';
  const initials = String(doctor.doctor_name || 'D')
    .split(/\s+/).slice(0, 2).map(name => name[0]).join('').toUpperCase();
  card.innerHTML = `
    <div class="doctor-avatar" aria-hidden="true">${escapeHtml(initials)}</div>
    <div class="doctor-result-info">
      <h3>${escapeHtml(doctor.doctor_name || 'Doctor')}</h3>
      <p class="doctor-specialty">${escapeHtml(doctor.specializations || 'General practice')}</p>
      <p>${escapeHtml(doctor.hospital_name || 'Hospital')} · ${Number(doctor.experience_years || 0)} years experience</p>
    </div>
    <div class="doctor-result-action">
      <span>${doctor.starting_fee == null ? 'Fees vary' : `From ₹${Number(doctor.starting_fee).toFixed(2)}`}</span>
      <a class="btn btn-secondary btn-sm" href="/patient/doctors/${encodeURIComponent(doctor.doctor_id)}">View &amp; book</a>
    </div>`;
  return card;
}

function initializeDoctorProfile(doctorId) {
  const tomorrow = new Date();
  tomorrow.setDate(tomorrow.getDate() + 1);
  const dateInput = document.getElementById('bookingDate');
  dateInput.min = toDateInputValue(new Date());
  dateInput.value = toDateInputValue(tomorrow);
  document.getElementById('consultationSelect').addEventListener('change', event => {
    selectedConsultation = event.target.value || null;
    loadDoctorSlots();
  });
  dateInput.addEventListener('change', loadDoctorSlots);
  document.getElementById('bookAppointmentButton').addEventListener('click', bookAppointment);
  loadDoctorProfile(doctorId);
}

async function loadDoctorProfile(doctorId) {
  const loading = document.getElementById('doctorProfileLoading');
  const content = document.getElementById('doctorProfileContent');
  const error = document.getElementById('doctorProfileError');
  loading.hidden = false;
  error.hidden = true;
  content.innerHTML = '';
  try {
    const data = await fetchJson(`/api/patient/doctors/${encodeURIComponent(doctorId)}`);
    selectedDoctor = data.doctor;
    document.getElementById('doctorProfileTitle').textContent = selectedDoctor.doctor_name || 'Doctor details';
    renderDoctorProfile(selectedDoctor);
    const selector = document.getElementById('consultationSelect');
    (selectedDoctor.consultations || []).forEach(consultation => {
      const option = document.createElement('option');
      option.value = consultation.consultation_type_id;
      option.textContent = `${consultation.name} · ${consultation.duration_minutes} min · ₹${Number(consultation.fee).toFixed(2)}`;
      selector.appendChild(option);
    });
    if (!selector.options.length || selector.options.length === 1) {
      error.textContent = 'This doctor has no active consultation types available.';
      error.hidden = false;
    }
  } catch (errorValue) {
    error.textContent = errorValue.message || 'Could not load doctor profile.';
    error.hidden = false;
  } finally {
    loading.hidden = true;
  }
}

function renderDoctorProfile(doctor) {
  const profile = document.getElementById('doctorProfileContent');
  const initials = String(doctor.doctor_name || 'D')
    .split(/\s+/).slice(0, 2).map(name => name[0]).join('').toUpperCase();
  profile.innerHTML = `
    <div class="doctor-profile-heading">
      <div class="doctor-avatar doctor-avatar-large" aria-hidden="true">${escapeHtml(initials)}</div>
      <div><h2>${escapeHtml(doctor.doctor_name || 'Doctor')}</h2><p class="doctor-specialty">${escapeHtml(doctor.specializations || 'General practice')}</p></div>
    </div>
    <div class="doctor-profile-facts">
      <div><span>Hospital</span><strong>${escapeHtml(doctor.hospital_name || 'Not listed')}</strong><small>${escapeHtml(doctor.hospital_address || '')}</small></div>
      <div><span>Experience</span><strong>${Number(doctor.experience_years || 0)} years</strong></div>
      <div><span>Qualifications</span><strong>${escapeHtml(doctor.qualification || 'Not listed')}</strong></div>
    </div>`;
  document.getElementById('slotTimezone').textContent = doctor.timezone ? `Times shown in ${doctor.timezone}` : '';
}

async function loadDoctorSlots() {
  const results = document.getElementById('slotResults');
  const loading = document.getElementById('slotLoading');
  const empty = document.getElementById('slotEmptyState');
  const error = document.getElementById('slotError');
  results.innerHTML = '';
  error.hidden = true;
  empty.hidden = true;
  selectedSlot = null;
  updateBookingButton();
  if (!selectedDoctor || !selectedConsultation || !document.getElementById('bookingDate').value) return;

  loading.hidden = false;
  const params = new URLSearchParams({
    date: document.getElementById('bookingDate').value,
    consultation_type_id: selectedConsultation,
  });
  try {
    const data = await fetchJson(
      `/api/patient/doctors/${encodeURIComponent(selectedDoctor.doctor_id)}/slots?${params}`
    );
    const slots = data.slots || [];
    document.getElementById('slotTimezone').textContent = data.timezone
      ? `Times shown in ${data.timezone}` : '';
    if (!slots.length) {
      empty.hidden = false;
      return;
    }
    slots.forEach(slot => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'patient-slot';
      button.textContent = formatSlotTime(slot.start_time, data.timezone);
      button.setAttribute('aria-pressed', 'false');
      button.addEventListener('click', () => {
        selectedSlot = slot;
        results.querySelectorAll('.patient-slot').forEach(item => {
          item.classList.remove('selected');
          item.setAttribute('aria-pressed', 'false');
        });
        button.classList.add('selected');
        button.setAttribute('aria-pressed', 'true');
        updateBookingButton();
      });
      results.appendChild(button);
    });
  } catch (errorValue) {
    error.textContent = errorValue.message || 'Could not load available times.';
    error.hidden = false;
  } finally {
    loading.hidden = true;
  }
}

async function bookAppointment() {
  if (!selectedDoctor || !selectedConsultation || !selectedSlot) return;
  const button = document.getElementById('bookAppointmentButton');
  button.disabled = true;
  button.textContent = 'Booking...';
  try {
    const data = await fetchJson('/api/patient/book-appointment', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        consultation_type_id: selectedConsultation,
        appointment_datetime: selectedSlot.start_time,
      }),
    });
    showNotice(`Appointment booked for ${formatSlotTime(data.appointment_datetime, selectedDoctor.timezone)}.`, 'success');
    selectedSlot = null;
    await loadDoctorSlots();
  } catch (error) {
    showNotice(error.message || 'Could not book this appointment.', 'danger');
    await loadDoctorSlots();
  } finally {
    button.textContent = 'Book appointment';
    updateBookingButton();
  }
}

function updateBookingButton() {
  const button = document.getElementById('bookAppointmentButton');
  if (!button) return;
  const text = document.getElementById('bookingSelectionText');
  button.disabled = !selectedSlot;
  if (!text) return;
  text.textContent = selectedSlot
    ? `${selectedDoctor.doctor_name} · ${formatSlotTime(selectedSlot.start_time, selectedDoctor.timezone)}`
    : 'Select an available time to continue.';
}

async function loadAppointments() {
  const loading = document.getElementById('patientAppointmentsLoading');
  const error = document.getElementById('patientAppointmentsError');
  const table = document.getElementById('patientAppointmentsContainer');
  const empty = document.getElementById('patientAppointmentsEmpty');
  const body = document.getElementById('patientAppointmentsBody');
  loading.hidden = false;
  error.hidden = true;
  table.hidden = true;
  empty.hidden = true;
  body.innerHTML = '';
  try {
    const data = await fetchJson('/api/patient/dashboard');
    setPatientName(data.patient?.full_name);
    if (!(data.appointments || []).length) {
      empty.hidden = false;
      return;
    }
    data.appointments.forEach(appointment => body.appendChild(createAppointmentRow(appointment)));
    table.hidden = false;
  } catch (errorValue) {
    error.textContent = errorValue.message || 'Could not load your appointments.';
    error.hidden = false;
  } finally {
    loading.hidden = true;
  }
}

function createAppointmentRow(appointment) {
  const row = document.createElement('tr');
  const status = String(appointment.status || 'UNKNOWN');
  const statusClass = status === 'CONFIRMED' ? 'badge-open'
    : status === 'COMPLETED' ? 'badge-primary' : 'badge-secondary';
  row.innerHTML = `
    <td data-label="Date and time">${escapeHtml(formatDateTime(appointment.slot_start_utc))}</td>
    <td data-label="Doctor">${escapeHtml(appointment.doctor_name || 'Doctor')}</td>
    <td data-label="Hospital">${escapeHtml([appointment.hospital_name, appointment.hospital_city].filter(Boolean).join(', ') || '—')}</td>
    <td data-label="Consultation">${escapeHtml(appointment.consultation_type || 'Consultation')}</td>
    <td data-label="Status"><span class="badge ${statusClass}">${escapeHtml(status.replaceAll('_', ' '))}</span></td>
    <td data-label="Fee">₹${Number(appointment.fee_amount || 0).toFixed(2)}</td>`;
  return row;
}

function createAppointmentItem(appointment) {
  const item = document.createElement('article');
  item.className = 'patient-appointment-item';
  item.innerHTML = `
    <div class="patient-appointment-date">${escapeHtml(formatDateTime(appointment.slot_start_utc))}</div>
    <div class="patient-appointment-details">
      <strong>${escapeHtml(appointment.doctor_name || 'Doctor')}</strong>
      <span>${escapeHtml(appointment.consultation_type || 'Consultation')} · ${escapeHtml(appointment.hospital_name || 'Hospital')}</span>
    </div>
    <span class="badge ${appointment.status === 'CONFIRMED' ? 'badge-open' : 'badge-secondary'}">${escapeHtml(String(appointment.status || 'UNKNOWN').replaceAll('_', ' '))}</span>`;
  return item;
}

async function fetchJson(url, options = {}) {
  const response = await fetch(url, options);
  let data;
  try {
    data = await response.json();
  } catch (error) {
    data = {};
  }
  if (!response.ok) throw new Error(data.message || `Request failed (${response.status})`);
  return data;
}

function setPatientName(name) {
  const label = name || 'Patient';
  const sidebarName = document.getElementById('sidebarPatientName');
  if (sidebarName) sidebarName.textContent = label;
}

function showNotice(message, type) {
  const notice = document.getElementById('patientNotice');
  notice.className = `patient-notice patient-notice-${type}`;
  notice.textContent = message;
  notice.hidden = false;
  notice.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function toDateInputValue(value) {
  const year = value.getFullYear();
  const month = String(value.getMonth() + 1).padStart(2, '0');
  const day = String(value.getDate()).padStart(2, '0');
  return `${year}-${month}-${day}`;
}

function normalizeDateTime(value) {
  const text = String(value || '').replace(' ', 'T');
  return /(?:Z|[+-]\d{2}:?\d{2})$/i.test(text) ? text : `${text}Z`;
}

function formatDateTime(value) {
  if (!value) return 'Time unavailable';
  const date = new Date(normalizeDateTime(value));
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
}

function formatSlotTime(value, timezone) {
  if (!value) return 'Time unavailable';
  const date = new Date(normalizeDateTime(value));
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleTimeString(undefined, {
    timeZone: timezone || undefined,
    hour: 'numeric', minute: '2-digit',
  });
}

function escapeHtml(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}