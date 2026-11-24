const DOCTORS_PER_PAGE = 8;
const AVAILABILITY_WINDOW_DAYS = 30;
let doctorPage = 1;
let doctorTotal = 0;
let selectedDoctor = null;
let selectedConsultation = null;
let selectedSlot = null;
let availabilityWeek = 0;
let availabilityStart = null;
let currentWeekSlots = [];

async function initializePatientProfile() {
    const form = document.getElementById('patientProfileForm');
    if (!form) return;

    try {
        const response = await fetch('/api/patient/profile', { headers: { Accept: 'application/json' } });
        const data = await response.json();
        if (!response.ok) throw new Error(data.message || 'Unable to load profile');
        const patient = data.patient || {};
        document.getElementById('firstName').value = patient.first_name || '';
        document.getElementById('lastName').value = patient.last_name || '';
        document.getElementById('email').value = patient.email || '';
        document.getElementById('phoneNumber').value = patient.phone_number || '';
        document.getElementById('dateOfBirth').value = patient.date_of_birth || '';
        document.getElementById('gender').value = patient.gender || '';
        document.getElementById('emergencyContactName').value = patient.emergency_contact_name || '';
        document.getElementById('emergencyContactPhone').value = patient.emergency_contact_phone || '';

        form.addEventListener('submit', async (event) => {
            event.preventDefault();
            const payload = Object.fromEntries(new FormData(form).entries());
            const saveResponse = await fetch('/api/patient/profile', {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
                body: JSON.stringify(payload),
            });
            const saveData = await saveResponse.json();
            if (!saveResponse.ok) throw new Error(saveData.message || 'Unable to save profile');
            window.MediSlotUI?.showNotice?.('Profile updated.', 'success');
        });

        document.getElementById('deactivatePatientAccount').addEventListener('click', async () => {
            if (!window.confirm('Deactivate your MediSlot patient account?')) return;
            const deactivateResponse = await fetch('/api/patient/profile', { method: 'DELETE' });
            const deactivateData = await deactivateResponse.json();
            if (!deactivateResponse.ok) throw new Error(deactivateData.message || 'Unable to deactivate account');
            window.location.assign('/login');
        });
    } catch (error) {
        window.MediSlotUI?.showNotice?.(error.message, 'danger');
    }
}
let currentWeekFrom = null;
let currentWeekDayCount = 0;

document.addEventListener('DOMContentLoaded', () => {
  const screen = document.querySelector('[data-patient-page]');
  if (!screen) return;

  const page = screen.dataset.patientPage;
  if (page === 'dashboard') loadDashboard();
  else loadPatientIdentity();

  if (page === 'profile') initializePatientProfile();
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
  loadDoctorSearchOptions()
    .catch(error => {
      const optionsError = document.getElementById('doctorSearchOptionsError');
      optionsError.textContent = error.message || 'Could not load search filters.';
      optionsError.hidden = false;
    })
    .finally(loadDoctors);
}

async function loadDoctorSearchOptions() {
  const data = await fetchJson('/api/patient/doctors/search-options');
  const specialization = document.getElementById('specializationInput');
  const hospital = document.getElementById('hospitalInput');
  const city = document.getElementById('cityInput');

  (data.specializations || []).forEach(name => {
    specialization.add(new Option(name, name));
  });
  (data.hospitals || []).forEach(item => {
    hospital.add(new Option(
      item.city ? `${item.name} · ${item.city}` : item.name,
      item.hospital_id,
    ));
  });
  (data.cities || []).forEach(name => city.add(new Option(name, name)));
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
    city: document.getElementById('cityInput').value.trim(),
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
      <span>${doctor.starting_fee == null ? 'Fees vary' : `From ${MediSlotMoney.formatINR(doctor.starting_fee)}`}</span>
      <a class="btn btn-secondary btn-sm" href="/patient/doctors/${encodeURIComponent(doctor.doctor_id)}">View &amp; book</a>
    </div>`;
  return card;
}

function initializeDoctorProfile(doctorId) {
  availabilityStart = toDateInputValue(new Date());
  document.getElementById('consultationSelect').addEventListener('change', event => {
    selectedConsultation = event.target.value || null;
    loadDoctorSlots();
  });
  document.getElementById('onlyFreeSeats').addEventListener('change', renderDoctorSlots);
  document.getElementById('previousAvailabilityWeek').addEventListener('click', () => {
    if (availabilityWeek > 0) {
      availabilityWeek--;
      loadDoctorSlots();
    }
  });
  document.getElementById('nextAvailabilityWeek').addEventListener('click', () => {
    if ((availabilityWeek + 1) * 7 < AVAILABILITY_WINDOW_DAYS) {
      availabilityWeek++;
      loadDoctorSlots();
    }
  });
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
    availabilityStart = dateKeyInTimezone(
      new Date().toISOString(),
      selectedDoctor.timezone || 'UTC',
    );
    document.getElementById('doctorProfileTitle').textContent = selectedDoctor.doctor_name || 'Doctor details';
    renderDoctorProfile(selectedDoctor);
    const selector = document.getElementById('consultationSelect');
    (selectedDoctor.consultations || []).forEach(consultation => {
      selector.add(new Option(
        `${consultation.name} · ${consultation.duration_minutes} min · ${MediSlotMoney.formatINR(consultation.fee)}`,
        consultation.consultation_type_id,
      ));
    });
    if (selector.options.length > 1) {
      selectedConsultation = selector.value || null;
      await loadDoctorSlots();
    } else {
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
  const loading = document.getElementById('slotLoading');
  const empty = document.getElementById('slotEmptyState');
  const error = document.getElementById('slotError');
  error.hidden = true;
  empty.hidden = true;
  selectedSlot = null;
  updateBookingButton();
  if (!selectedDoctor) return;

  loading.hidden = false;
  const firstOffset = availabilityWeek * 7;
  const dayCount = Math.min(7, AVAILABILITY_WINDOW_DAYS - firstOffset);
  const from = addCalendarDays(availabilityStart, firstOffset);
  const to = addCalendarDays(from, dayCount - 1);
  const weekStartDate = new Date(`${from}T12:00:00`);
  const weekEndDate = new Date(`${to}T12:00:00`);
  document.getElementById('availabilityWeekLabel').textContent =
    new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric' })
      .formatRange(weekStartDate, weekEndDate);
  document.getElementById('previousAvailabilityWeek').disabled = availabilityWeek === 0;
  document.getElementById('nextAvailabilityWeek').disabled =
    firstOffset + dayCount >= AVAILABILITY_WINDOW_DAYS;
  const params = new URLSearchParams({
    doctor_id: selectedDoctor.doctor_id,
    from,
    to,
    include_full: 'true',
  });
  if (selectedConsultation) params.set('consultation_type_id', selectedConsultation);
  try {
    const data = await fetchJson(`/api/slots?${params}`);
    document.getElementById('slotTimezone').textContent = data.timezone
      ? `Times shown in ${data.timezone}` : '';
    selectedDoctor.timezone = data.timezone || selectedDoctor.timezone;
    currentWeekSlots = data.slots || [];
    currentWeekFrom = from;
    currentWeekDayCount = dayCount;
    renderDoctorSlots();
  } catch (errorValue) {
    error.textContent = errorValue.message || 'Could not load available times.';
    error.hidden = false;
  } finally {
    loading.hidden = true;
  }
}

function renderDoctorSlots() {
  const results = document.getElementById('slotResults');
  const empty = document.getElementById('slotEmptyState');
  const slots = currentWeekSlots;
  const from = currentWeekFrom;
  const dayCount = currentWeekDayCount;
  if (!from) return;
  const onlyFreeSeats = document.getElementById('onlyFreeSeats').checked;
  const visibleSlots = slots.filter(slot =>
    !selectedConsultation || slot.consultation_type_id === selectedConsultation
  ).filter(slot => !onlyFreeSeats || Number(slot.free_seats) > 0);
  results.replaceChildren();

  const openSlots = slots.filter(slot => Number(slot.free_seats) > 0);
  document.getElementById('openSlotCount').textContent =
    `${openSlots.length} open ${openSlots.length === 1 ? 'slot' : 'slots'}`;
  document.getElementById('freeSeatCount').textContent =
    `${openSlots.reduce((total, slot) => total + Number(slot.free_seats || 0), 0)} free seats`;
  empty.hidden = visibleSlots.length > 0;
  if (!visibleSlots.length) return;

  for (let dayOffset = 0; dayOffset < dayCount; dayOffset++) {
    const dateKey = addCalendarDays(from, dayOffset);
    const daySlots = visibleSlots.filter(slot =>
      dateKeyInTimezone(slot.start_time, selectedDoctor.timezone || 'UTC') === dateKey
    ).sort((left, right) => left.start_time.localeCompare(right.start_time));
    if (!daySlots.length) continue;

    const group = document.createElement('section');
    group.className = 'patient-slot-day';
    const heading = document.createElement('div');
    heading.className = 'patient-slot-day-heading';
    const title = document.createElement('h3');
    title.textContent = new Intl.DateTimeFormat(undefined, {
      weekday: 'long', month: 'short', day: 'numeric',
    }).format(new Date(`${dateKey}T12:00:00`));
    const seats = document.createElement('span');
    const totalFree = daySlots.reduce((total, slot) => total + Number(slot.free_seats || 0), 0);
    seats.textContent = `${totalFree} free ${totalFree === 1 ? 'seat' : 'seats'}`;
    heading.append(title, seats);

    const cards = document.createElement('div');
    cards.className = 'patient-slot-cards';
    daySlots.forEach(slot => cards.appendChild(createPatientSlotCard(slot)));
    group.append(heading, cards);
    results.appendChild(group);
  }
}

function createPatientSlotCard(slot) {
  const wrapper = document.createElement('article');
  wrapper.className = 'patient-slot-card';
  const select = document.createElement('button');
  select.type = 'button';
  select.className = `patient-slot-select${Number(slot.free_seats) > 0 ? '' : ' full'}`;
  select.disabled = Number(slot.free_seats) <= 0;
  select.setAttribute('aria-pressed', 'false');
  const consultation = (selectedDoctor.consultations || []).find(item =>
    item.consultation_type_id === slot.consultation_type_id
  );
  const name = document.createElement('strong');
  name.textContent = formatSlotTime(slot.start_time, selectedDoctor.timezone);
  const details = document.createElement('span');
  details.textContent = `${consultation?.name || 'Consultation'} · ${MediSlotMoney.formatINR(consultation?.fee || 0)}`;
  const free = document.createElement('small');
  free.textContent = Number(slot.free_seats) > 0
    ? `${slot.free_seats} ${Number(slot.free_seats) === 1 ? 'seat' : 'seats'} free`
    : 'Full';
  select.append(name, details, free);
  select.addEventListener('click', () => {
    selectedSlot = slot;
    document.querySelectorAll('.patient-slot-select').forEach(button => {
      const selected = button === select;
      button.classList.toggle('selected', selected);
      button.setAttribute('aria-pressed', String(selected));
    });
    updateBookingButton();
  });
  wrapper.appendChild(select);

  if (Number(slot.free_seats) <= 0) {
    const waitlistButton = document.createElement('button');
    waitlistButton.type = 'button';
    waitlistButton.className = 'btn btn-secondary btn-sm';
    waitlistButton.textContent = 'Join waitlist';
    waitlistButton.addEventListener('click', () => joinSlotWaitlist(slot));
    wrapper.appendChild(waitlistButton);
  }
  return wrapper;
}

async function joinSlotWaitlist(slot) {
  try {
    await fetchJson('/api/patient/waitlists', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ slot_id: slot.slot_id }),
    });
    showNotice('You joined the waitlist. We will notify you if this time opens.', 'success');
    await loadWaitlists();
  } catch (error) {
    showNotice(error.message || 'Could not join this waitlist.', 'danger');
  }
}

async function bookAppointment() {
  if (!selectedDoctor || !selectedSlot) return;
  const button = document.getElementById('bookAppointmentButton');
  button.disabled = true;
  button.textContent = 'Booking...';
  try {
    const data = await fetchJson('/api/patient/book-appointment', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Idempotency-Key': crypto.randomUUID(),
      },
      body: JSON.stringify({
        consultation_type_id: selectedSlot.consultation_type_id,
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

function addCalendarDays(dateKey, days) {
  const date = new Date(`${dateKey}T12:00:00Z`);
  date.setUTCDate(date.getUTCDate() + days);
  return date.toISOString().slice(0, 10);
}

function dateKeyInTimezone(value, timezone) {
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: timezone,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).formatToParts(new Date(normalizeDateTime(value)));
  const values = Object.fromEntries(parts.map(part => [part.type, part.value]));
  return `${values.year}-${values.month}-${values.day}`;
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
    await loadWaitlists();
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
  const cells = [
    ['Date and time', formatDateTime(appointment.slot_start_utc)],
    ['Doctor', appointment.doctor_name || 'Doctor'],
    ['Hospital', [appointment.hospital_name, appointment.hospital_city].filter(Boolean).join(', ') || '—'],
    ['Consultation', appointment.consultation_type || 'Consultation'],
  ];
  cells.forEach(([label, value]) => {
    const cell = document.createElement('td');
    cell.dataset.label = label;
    cell.textContent = value;
    row.appendChild(cell);
  });
  const statusCell = document.createElement('td');
  statusCell.dataset.label = 'Status';
  const badge = document.createElement('span');
  badge.className = `badge ${statusClass}`;
  badge.textContent = status.replaceAll('_', ' ');
  statusCell.appendChild(badge);
  row.appendChild(statusCell);

  const paymentCell = document.createElement('td');
  paymentCell.dataset.label = 'Fee / payment';
  paymentCell.textContent = `${MediSlotMoney.formatINR(appointment.fee_amount || 0)} · ${appointment.payment_status || 'NOT RECORDED'} (${String(appointment.payment_method || 'PAY_AT_CLINIC').replaceAll('_', ' ').toLowerCase()})`;
  row.appendChild(paymentCell);

  const refundCell = document.createElement('td');
  refundCell.dataset.label = 'Refund';
  refundCell.textContent = MediSlotMoney.formatINR(appointment.refund_amount || 0);
  row.appendChild(refundCell);

  const actionsCell = document.createElement('td');
  actionsCell.dataset.label = 'Actions';
  if (status === 'CONFIRMED') {
    const cancelButton = document.createElement('button');
    cancelButton.type = 'button';
    cancelButton.className = 'btn btn-secondary btn-sm';
    cancelButton.textContent = 'Refund preview & cancel';
    cancelButton.addEventListener('click', () => cancelAppointment(appointment));
    actionsCell.appendChild(cancelButton);
  } else {
    actionsCell.textContent = '—';
  }
  row.appendChild(actionsCell);
  return row;
}

async function cancelAppointment(appointment) {
  try {
    const quote = await fetchJson(
      `/api/patient/appointments/${encodeURIComponent(appointment.id)}/refund-preview`,
    );
    const confirmed = window.confirm(
      `Cancel this appointment?\n\nEstimated refund ledger: ${MediSlotMoney.formatINR(quote.refund_amount)} (${quote.refund_percent}% — ${String(quote.refund_tier).replaceAll('_', ' ')}).\n\nMediSlot does not process payments; any refund is recorded for clinic processing.`,
    );
    if (!confirmed) return;
    const result = await fetchJson(
      `/api/patient/appointments/${encodeURIComponent(appointment.id)}/cancel`,
      { method: 'POST' },
    );
    showNotice(
      `Appointment cancelled. Refund ledger: ${MediSlotMoney.formatINR(result.refund_amount)}.`,
      'success',
    );
    await loadAppointments();
  } catch (error) {
    showNotice(error.message || 'Could not cancel this appointment.', 'danger');
  }
}

async function loadWaitlists() {
  const error = document.getElementById('patientWaitlistError');
  const list = document.getElementById('patientWaitlists');
  const empty = document.getElementById('patientWaitlistsEmpty');
  if (!list || !empty) return;
  error.hidden = true;
  list.replaceChildren();
  empty.hidden = true;
  try {
    const data = await fetchJson('/api/patient/waitlists');
    const waitlists = data.waitlists || [];
    const offers = data.offers || [];
    waitlists.forEach(item => list.appendChild(createWaitlistItem(item)));
    offers.filter(item => item.status === 'OPEN').forEach(item => list.appendChild(createOfferItem(item)));
    empty.hidden = waitlists.length + offers.filter(item => item.status === 'OPEN').length > 0;
  } catch (errorValue) {
    error.textContent = errorValue.message || 'Could not load waitlists and offers.';
    error.hidden = false;
  }
}

function createWaitlistItem(entry) {
  const item = document.createElement('article');
  item.className = 'patient-appointment-item';
  const details = document.createElement('div');
  details.className = 'patient-appointment-details';
  const title = document.createElement('strong');
  title.textContent = 'Waitlist';
  const time = document.createElement('span');
  time.textContent = formatDateTime(entry.slot_start);
  details.append(title, time);
  const leaveButton = document.createElement('button');
  leaveButton.type = 'button';
  leaveButton.className = 'btn btn-secondary btn-sm';
  leaveButton.textContent = 'Leave waitlist';
  leaveButton.addEventListener('click', async () => {
    try {
      await fetchJson('/api/patient/waitlists', {
        method: 'DELETE',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ slot_id: entry.slot_ref }),
      });
      await loadWaitlists();
    } catch (error) {
      showNotice(error.message || 'Could not leave this waitlist.', 'danger');
    }
  });
  item.append(details, leaveButton);
  return item;
}

function createOfferItem(offer) {
  const item = document.createElement('article');
  item.className = 'patient-appointment-item';
  const details = document.createElement('div');
  details.className = 'patient-appointment-details';
  const title = document.createElement('strong');
  title.textContent = 'A slot is available for you';
  const time = document.createElement('span');
  time.textContent = `${formatDateTime(offer.slot_start)} · expires ${new Date(Number(offer.expires_at) * 1000).toLocaleTimeString()}`;
  details.append(title, time);
  const acceptButton = document.createElement('button');
  acceptButton.type = 'button';
  acceptButton.className = 'btn btn-teal btn-sm';
  acceptButton.textContent = 'Accept offer';
  acceptButton.addEventListener('click', async () => {
    acceptButton.disabled = true;
    try {
      const booking = await fetchJson(
        `/api/patient/waitlist-offers/${encodeURIComponent(offer.offer_id)}/accept`,
        { method: 'POST' },
      );
      showNotice(`Appointment booked for ${formatDateTime(booking.appointment_datetime)}.`, 'success');
      await loadAppointments();
    } catch (error) {
      showNotice(error.message || 'Could not accept this offer.', 'danger');
      acceptButton.disabled = false;
    }
  });
  item.append(details, acceptButton);
  return item;
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
  return MediSlotTime.normalize(value);
}

function formatDateTime(value) {
  return MediSlotTime.formatDateTime(value);
}

function formatSlotTime(value, timezone) {
  return MediSlotTime.formatTime(value, timezone);
}

function escapeHtml(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}