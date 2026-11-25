const DOCTORS_PER_PAGE = 8;
const AVAILABILITY_WINDOW_DAYS = 30;
const PENDING_BOOKING_STORAGE_KEY = 'medislotPendingBooking';
let doctorPage = 1;
let doctorTotal = 0;
let selectedDoctor = null;
let selectedConsultation = null;
let selectedSlot = null;
let availabilityWeek = 0;
let availabilityStart = null;
let currentWeekSlots = [];
let patientAppointments = [];
let pendingRefundAppointment = null;
let activeBookingRequestId = sessionStorage.getItem('medislotPendingBookingRequest');

async function initializePatientProfile() {
    const form = document.getElementById('patientProfileForm');
    if (!form) return;

    try {
        const data = await fetchJson('/api/patient/profile');
        const patient = data.patient || {};
        const fields = profileFields();
        fields.forEach(field => {
            const element = document.getElementById(field);
            const key = element.name || field;
            element.value = patient[key] || '';
        });
        document.getElementById('email').value = patient.email || '';
        setPatientName([patient.first_name, patient.last_name].filter(Boolean).join(' '));
        document.getElementById('patientProfileHeading').textContent =
            [patient.first_name, patient.last_name].filter(Boolean).join(' ') || 'My profile';
        document.getElementById('patientProfileAvatar').textContent =
            `${patient.first_name?.[0] || ''}${patient.last_name?.[0] || ''}`.toUpperCase() || 'P';

        const editButton = document.getElementById('editPatientProfile');
        const cancelButton = document.getElementById('cancelPatientProfileEdit');
        const actions = document.getElementById('patientProfileActions');
        const saveButton = document.getElementById('savePatientProfile');
        let originalValues = profileValues(form);
        const setEditing = (editing) => {
            fields.forEach(field => {
                const element = document.getElementById(field);
                if (element) {
                    if (element.tagName === 'SELECT') element.disabled = !editing;
                    else element.readOnly = !editing;
                }
            });
            actions.hidden = !editing;
            editButton.hidden = editing;
        };
        editButton.addEventListener('click', () => {
            originalValues = profileValues(form);
            setEditing(true);
            document.getElementById('firstName').focus();
        });
        cancelButton.addEventListener('click', () => {
            restoreProfileValues(form, originalValues);
            setEditing(false);
        });
        form.addEventListener('submit', async (event) => {
            event.preventDefault();
            saveButton.disabled = true;
            try {
                const payload = Object.fromEntries(new FormData(form).entries());
                await fetchJson('/api/patient/profile', {
                    method: 'PATCH',
                    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
                    body: JSON.stringify(payload),
                });
                originalValues = profileValues(form);
                setEditing(false);
                showNotice('Your profile has been updated.', 'success');
            } catch (error) {
                showNotice(error.message || 'Unable to save profile.', 'danger');
            } finally {
                saveButton.disabled = false;
            }
        });

        document.getElementById('deactivatePatientAccount').addEventListener('click', async () => {
            if (!window.confirm('Deactivate your MediSlot patient account?')) return;
            await fetchJson('/api/patient/profile', { method: 'DELETE' });
            window.location.assign('/login');
        });
    } catch (error) {
        showNotice(error.message || 'Unable to load profile.', 'danger');
    }
}

function profileFields() {
    return ['firstName', 'lastName', 'phoneNumber', 'dateOfBirth', 'gender',
        'emergencyContactName', 'emergencyContactPhone'];
}

function profileValues(form) {
    return Object.fromEntries(profileFields().map(id => [id, document.getElementById(id).value]));
}

function restoreProfileValues(form, values) {
    Object.entries(values).forEach(([id, value]) => {
        document.getElementById(id).value = value;
    });
}
let currentWeekFrom = null;
let currentWeekDayCount = 0;

document.addEventListener('DOMContentLoaded', () => {
  const screen = document.querySelector('[data-patient-page]');
  if (!screen) return;

  document.querySelectorAll('[data-close-dialog]').forEach(button =>
    button.addEventListener('click', () => document.getElementById('appointmentDetailsDialog').close())
  );
  const appointmentDetailsDialog = document.getElementById('appointmentDetailsDialog');
  appointmentDetailsDialog?.addEventListener('click', event => {
    if (event.target === appointmentDetailsDialog) appointmentDetailsDialog.close();
  });

  const page = screen.dataset.patientPage;
  if (page === 'dashboard') loadDashboard();
  else loadPatientIdentity();

  if (page === 'profile') initializePatientProfile();
  if (page === 'doctors') initializeDoctorSearch();
  if (page === 'doctor-profile') {
    initializeDoctorProfile(screen.dataset.doctorId);
    if (activeBookingRequestId || readPendingBooking()) resumePendingBooking();
  }
  if (page === 'appointments') {
    document.getElementById('refreshAppointments').addEventListener('click', loadAppointments);
    initializeAppointmentFilters();
    initializeRefundDialog();
    loadAppointments();
  }
  if (page === 'payments') initializePatientPayments();
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
    const upcoming = appointments.filter(item =>
      item.status === 'CONFIRMED' && new Date(normalizeDateTime(item.slot_start_utc)) >= new Date()
    ).sort((a, b) => new Date(normalizeDateTime(a.slot_start_utc)) - new Date(normalizeDateTime(b.slot_start_utc)));
    const nextAppointment = upcoming[0];
    document.getElementById('nextAppointmentContent').innerHTML = nextAppointment
      ? `<strong>${escapeHtml(nextAppointment.doctor_name || 'Doctor')}</strong>
         <span>${escapeHtml(formatDateTime(nextAppointment.slot_start_utc))}</span>
         <span>${escapeHtml(nextAppointment.hospital_name || '')}</span>`
      : '<span>No upcoming visits</span><a class="patient-text-link" href="/patient/doctors">Find a doctor</a>';

    if (!upcoming.length) {
      empty.hidden = false;
      return;
    }
    upcoming.slice(0, 4).forEach(appointment => list.appendChild(createAppointmentItem(appointment)));
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
    waitlistButton.disabled = true;
    waitlistButton.title = 'Waitlist is currently unavailable';
    waitlistButton.setAttribute('aria-label', 'Join waitlist, currently unavailable');
    wrapper.appendChild(waitlistButton);
  }
  return wrapper;
}

async function bookAppointment() {
  if (!selectedDoctor || !selectedSlot) return;
  const button = document.getElementById('bookAppointmentButton');
  if (activeBookingRequestId) return;
  const bookingPayload = {
    consultation_type_id: selectedSlot.consultation_type_id,
    appointment_datetime: selectedSlot.start_time,
  };
  const previousRequest = readPendingBooking();
  if (previousRequest && !sameBookingPayload(previousRequest, bookingPayload)) return;
  const pendingRequest = {
    ...(previousRequest || {}),
    idempotencyKey: previousRequest?.idempotencyKey || createIdempotencyKey(),
    ...bookingPayload,
  };
  savePendingBooking(pendingRequest);
  button.disabled = true;
  button.textContent = 'Requesting...';
  try {
    const request = await fetchJson('/api/patient/book-appointment', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Idempotency-Key': pendingRequest.idempotencyKey,
      },
      body: JSON.stringify(bookingPayload),
    });
    if (request.status === 'PENDING' && request.request_id) {
      activeBookingRequestId = request.request_id;
      savePendingBooking({ ...pendingRequest, requestId: activeBookingRequestId });
    }
    let booking = request;
    if (request.status === 'PENDING' && request.request_id) {
      showNotice('Your booking request is queued. Checking its status…', 'info');
      booking = await waitForBookingResult(request.request_id);
    }
    if (booking.status === 'PENDING') {
      showNotice(
        `Booking request ${activeBookingRequestId} is still processing. Do not submit another request.`,
        'info',
      );
      selectedSlot = null;
      return;
    }
    clearPendingBooking();
    if (booking.status === 'DECLINED') {
      throw new Error(booking.message || 'The booking request was declined.');
    }
    if (booking.status !== 'CONFIRMED') {
      showNotice('Your booking is still processing. You can check its status in a moment.', 'info');
      return;
    }
    showNotice(
      `Appointment confirmed for ${formatSlotTime(selectedSlot.start_time, selectedDoctor.timezone)}.`,
      'success',
    );
    selectedSlot = null;
    await loadDoctorSlots();
  } catch (error) {
    if (error.data?.status === 'DECLINED') {
      clearPendingBooking();
    }
    showNotice(error.message || 'Could not book this appointment.', 'danger');
    await loadDoctorSlots();
  } finally {
    button.textContent = 'Book appointment';
    updateBookingButton();
  }
}

function createIdempotencyKey() {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  if (typeof crypto !== 'undefined' && typeof crypto.getRandomValues === 'function') {
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    return Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('');
  }
  return `${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

async function waitForBookingResult(requestId) {
  for (let attempt = 0; attempt < 30; attempt += 1) {
    await new Promise(resolve => setTimeout(resolve, 1000));
    const response = await fetchJson(
      `/api/patient/booking-requests/${encodeURIComponent(requestId)}`,
    );
    const booking = response.booking_request || {};
    if (booking.status === 'CONFIRMED' || booking.status === 'DECLINED') {
      return {
        ...booking,
        message: booking.result_message || booking.message,
      };
    }
  }
  return { status: 'PENDING', request_id: requestId };
}

async function resumePendingBooking() {
  try {
    let requestId = activeBookingRequestId;
    let result;
    if (!requestId) {
      const pendingRequest = readPendingBooking();
      if (!pendingRequest) return;
      const request = await fetchJson('/api/patient/book-appointment', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Idempotency-Key': pendingRequest.idempotencyKey,
        },
        body: JSON.stringify({
          consultation_type_id: pendingRequest.consultation_type_id,
          appointment_datetime: pendingRequest.appointment_datetime,
        }),
      });
      if (request.status === 'CONFIRMED' || request.status === 'DECLINED') {
        requestId = request.request_id;
        result = request;
      } else if (request.request_id) {
        requestId = request.request_id;
        activeBookingRequestId = requestId;
        savePendingBooking({ ...pendingRequest, requestId });
        result = await waitForBookingResult(requestId);
      } else {
        result = { status: 'PENDING', request_id: requestId };
      }
    } else {
      result = await waitForBookingResult(requestId);
    }
    if (result.status === 'PENDING') {
      showNotice(
        `Booking request ${requestId || 'with an uncertain response'} is still processing. Do not submit another request.`,
        'info',
      );
      return;
    }
    clearPendingBooking();
    if (result.status === 'CONFIRMED') {
      showNotice(`Booking request ${requestId} is confirmed.`, 'success');
    } else {
      showNotice(result.message || 'The booking request was declined.', 'danger');
    }
  } catch (error) {
    if (error.data?.status === 'DECLINED') {
      clearPendingBooking();
    }
    showNotice(error.message || 'Could not check the booking request status.', 'danger');
  }
}

function readPendingBooking() {
  const stored = sessionStorage.getItem(PENDING_BOOKING_STORAGE_KEY);
  if (!stored) return null;
  let pending;
  try {
    pending = JSON.parse(stored);
  } catch {
    sessionStorage.removeItem(PENDING_BOOKING_STORAGE_KEY);
    return null;
  }
  if (
    pending
    && typeof pending === 'object'
    && typeof pending.idempotencyKey === 'string'
    && typeof pending.consultation_type_id === 'string'
    && typeof pending.appointment_datetime === 'string'
  ) {
    return pending;
  }
  sessionStorage.removeItem(PENDING_BOOKING_STORAGE_KEY);
  return null;
}

function savePendingBooking(pending) {
  sessionStorage.setItem(PENDING_BOOKING_STORAGE_KEY, JSON.stringify(pending));
  if (pending.requestId) {
    sessionStorage.setItem('medislotPendingBookingRequest', pending.requestId);
  }
}

function clearPendingBooking() {
  activeBookingRequestId = null;
  sessionStorage.removeItem('medislotPendingBookingRequest');
  sessionStorage.removeItem(PENDING_BOOKING_STORAGE_KEY);
}

function sameBookingPayload(pending, payload) {
  return pending.consultation_type_id === payload.consultation_type_id
    && pending.appointment_datetime === payload.appointment_datetime;
}

function updateBookingButton() {
  const button = document.getElementById('bookAppointmentButton');
  if (!button) return;
  const pendingRequest = readPendingBooking();
  const selectedPayload = selectedSlot && {
    consultation_type_id: selectedSlot.consultation_type_id,
    appointment_datetime: selectedSlot.start_time,
  };
  const differentPendingRequest = pendingRequest && selectedPayload
    && !sameBookingPayload(pendingRequest, selectedPayload);
  const text = document.getElementById('bookingSelectionText');
  const title = document.getElementById('bookingSelectionTitle');
  button.disabled = !selectedSlot || Boolean(activeBookingRequestId) || Boolean(differentPendingRequest);
  if (title) {
    title.textContent = selectedSlot
      ? `${selectedDoctor.doctor_name} · ${formatSlotTime(selectedSlot.start_time, selectedDoctor.timezone)}`
      : 'Choose a time';
  }
  if (text) {
    const consultation = selectedSlot && (selectedDoctor.consultations || []).find(item =>
      item.consultation_type_id === selectedSlot.consultation_type_id
    );
    text.textContent = selectedSlot
      ? `${consultation?.name || 'Consultation'} · ${MediSlotMoney.formatINR(consultation?.fee || 0)}`
      : 'Select an available slot to continue.';
  }
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
    patientAppointments = data.appointments || [];
    renderAppointments();
  } catch (errorValue) {
    error.textContent = errorValue.message || 'Could not load your appointments.';
    error.hidden = false;
  } finally {
    loading.hidden = true;
  }
}

function initializeAppointmentFilters() {
  ['appointmentSearch', 'appointmentStatusFilter', 'appointmentDateFrom', 'appointmentDateTo']
    .forEach(id => document.getElementById(id).addEventListener('input', renderAppointments));
  document.getElementById('clearAppointmentFilters').addEventListener('click', () => {
    ['appointmentSearch', 'appointmentStatusFilter', 'appointmentDateFrom', 'appointmentDateTo']
      .forEach(id => { document.getElementById(id).value = ''; });
    renderAppointments();
  });
}

function initializeRefundDialog() {
  const dialog = document.getElementById('refundPreviewDialog');
  document.querySelectorAll('[data-close-refund]').forEach(button =>
    button.addEventListener('click', () => dialog.close())
  );
  document.getElementById('confirmAppointmentCancellation').addEventListener('click', async event => {
    if (!pendingRefundAppointment) return;
    const button = event.currentTarget;
    const error = document.getElementById('refundDialogError');
    button.disabled = true;
    error.hidden = true;
    try {
      const result = await fetchJson(
        `/api/patient/appointments/${encodeURIComponent(pendingRefundAppointment.id)}/cancel`,
        { method: 'POST' },
      );
      dialog.close();
      pendingRefundAppointment = null;
      showNotice(`Appointment cancelled. Estimated refund: ${MediSlotMoney.formatINR(result.refund_amount)}.`, 'success');
      await loadAppointments();
    } catch (requestError) {
      error.textContent = requestError.message || 'Could not cancel this appointment.';
      error.hidden = false;
    } finally {
      button.disabled = false;
    }
  });
}

function renderAppointments() {
  const table = document.getElementById('patientAppointmentsContainer');
  const empty = document.getElementById('patientAppointmentsEmpty');
  const body = document.getElementById('patientAppointmentsBody');
  if (!table || !body) return;
  const query = document.getElementById('appointmentSearch').value.trim().toLowerCase();
  const status = document.getElementById('appointmentStatusFilter').value;
  const from = document.getElementById('appointmentDateFrom').value;
  const to = document.getElementById('appointmentDateTo').value;
  const filtered = patientAppointments.filter(appointment => {
    const text = [
      appointment.doctor_name, appointment.hospital_name, appointment.hospital_city,
      appointment.consultation_type, appointment.status,
    ].join(' ').toLowerCase();
    const date = normalizeDateTime(appointment.slot_start_utc).slice(0, 10);
    return (!query || text.includes(query))
      && (!status || appointment.status === status)
      && (!from || date >= from)
      && (!to || date <= to);
  });
  body.replaceChildren(...filtered.map(createAppointmentRow));
  table.hidden = filtered.length === 0;
  empty.hidden = filtered.length > 0;
}

function createAppointmentRow(appointment) {
  const row = document.createElement('tr');
  row.classList.add('patient-appointment-row');
  row.tabIndex = 0;
  row.setAttribute('aria-label', `View appointment details for ${appointment.doctor_name || 'doctor'}`);
  row.addEventListener('click', event => {
    if (!event.target.closest('button')) showAppointmentDetails(appointment);
  });
  row.addEventListener('keydown', event => {
    if ((event.key === 'Enter' || event.key === ' ') && !event.target.closest('button')) {
      event.preventDefault();
      showAppointmentDetails(appointment);
    }
  });
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
    cancelButton.className = 'btn btn-danger btn-sm patient-cancel-appointment';
    cancelButton.textContent = 'Cancel';
    cancelButton.addEventListener('click', () => cancelAppointment(appointment));
    actionsCell.appendChild(cancelButton);
  } else {
    const detailsButton = document.createElement('button');
    detailsButton.type = 'button';
    detailsButton.className = 'btn btn-secondary btn-sm';
    detailsButton.textContent = 'Details';
    detailsButton.addEventListener('click', () => showAppointmentDetails(appointment));
    actionsCell.appendChild(detailsButton);
  }
  row.appendChild(actionsCell);
  return row;
}

async function cancelAppointment(appointment) {
  try {
    const quote = await fetchJson(
      `/api/patient/appointments/${encodeURIComponent(appointment.id)}/refund-preview`,
    );
    pendingRefundAppointment = appointment;
    document.getElementById('refundAppointmentTime').textContent = formatDateTime(appointment.slot_start_utc);
    document.getElementById('refundAmount').textContent = MediSlotMoney.formatINR(quote.refund_amount || 0);
    document.getElementById('refundTier').textContent =
      `${String(quote.refund_tier || 'standard').replaceAll('_', ' ')} · ${quote.refund_percent ?? 0}%`;
    document.getElementById('refundDialogError').hidden = true;
    document.getElementById('refundPreviewDialog').showModal();
  } catch (error) {
    showNotice(error.message || 'Could not cancel this appointment.', 'danger');
  }
}

function createAppointmentItem(appointment) {
  const item = document.createElement('article');
  item.className = 'patient-appointment-item patient-appointment-clickable';
  const detailsButton = document.createElement('button');
  detailsButton.type = 'button';
  detailsButton.className = 'patient-appointment-open';
  detailsButton.setAttribute('aria-label', `View appointment with ${appointment.doctor_name || 'doctor'}`);
  detailsButton.innerHTML = `
    <span class="patient-appointment-date">${escapeHtml(formatDateTime(appointment.slot_start_utc))}</span>
    <span class="patient-appointment-details">
      <strong>${escapeHtml(appointment.doctor_name || 'Doctor')}</strong>
      <span>${escapeHtml(appointment.consultation_type || 'Consultation')} · ${escapeHtml(appointment.hospital_name || 'Hospital')}</span>
    </span>
    <span class="badge ${appointment.status === 'CONFIRMED' ? 'badge-open' : 'badge-secondary'}">${escapeHtml(String(appointment.status || 'UNKNOWN').replaceAll('_', ' '))}</span>`;
  detailsButton.addEventListener('click', () => showAppointmentDetails(appointment));
  item.appendChild(detailsButton);
  return item;
}

function showAppointmentDetails(appointment) {
  const body = document.getElementById('appointmentDetailsBody');
  if (!body) return;
  const details = [
    ['Doctor', appointment.doctor_name || 'Doctor'],
    ['Date and time', formatDateTime(appointment.slot_start_utc)],
    ['Hospital', [appointment.hospital_name, appointment.hospital_city].filter(Boolean).join(', ') || '—'],
    ['Consultation', appointment.consultation_type || 'Consultation'],
    ['Status', String(appointment.status || 'UNKNOWN').replaceAll('_', ' ')],
    ['Fee', MediSlotMoney.formatINR(appointment.fee_amount || 0)],
    ['Payment', `${appointment.payment_status || 'Not recorded'} · ${String(appointment.payment_method || 'PAY_AT_CLINIC').replaceAll('_', ' ').toLowerCase()}`],
    ['Refunds recorded', MediSlotMoney.formatINR(appointment.refund_amount || 0)],
  ];
  body.replaceChildren(...details.map(([label, value]) => {
    const row = document.createElement('div');
    row.className = 'patient-detail-row';
    const key = document.createElement('span');
    key.textContent = label;
    const content = document.createElement('strong');
    content.textContent = value;
    row.append(key, content);
    return row;
  }));
  document.getElementById('appointmentDetailsDialog').showModal();
}

function initializePatientPayments() {
  const filters = ['paymentSearch', 'paymentStatusFilter', 'paymentDateFrom', 'paymentDateTo'];
  filters.forEach(id => document.getElementById(id).addEventListener('input', renderPatientPayments));
  document.getElementById('clearPaymentFilters').addEventListener('click', () => {
    filters.forEach(id => { document.getElementById(id).value = ''; });
    renderPatientPayments();
  });
  document.getElementById('refreshPatientPayments').addEventListener('click', loadPatientPayments);
  const dialog = document.getElementById('patientPaymentDetailsDialog');
  document.querySelectorAll('[data-close-payment-dialog]').forEach(button =>
    button.addEventListener('click', () => dialog.close())
  );
  loadPatientPayments();
}

let patientPaymentAppointments = [];

async function loadPatientPayments() {
  const loading = document.getElementById('patientPaymentsLoading');
  const error = document.getElementById('patientPaymentsError');
  const table = document.getElementById('patientPaymentsContainer');
  const empty = document.getElementById('patientPaymentsEmpty');
  loading.hidden = false;
  error.hidden = true;
  table.hidden = true;
  empty.hidden = true;
  try {
    const data = await fetchJson('/api/patient/dashboard');
    setPatientName(data.patient?.full_name);
    patientPaymentAppointments = (data.appointments || []).filter(appointment =>
      appointment.payment_status || Number(appointment.fee_amount || 0) > 0
    );
    renderPatientPayments();
  } catch (requestError) {
    error.textContent = requestError.message || 'Could not load payment history.';
    error.hidden = false;
  } finally {
    loading.hidden = true;
  }
}

function renderPatientPayments() {
  const body = document.getElementById('patientPaymentsBody');
  if (!body) return;
  const query = document.getElementById('paymentSearch').value.trim().toLowerCase();
  const status = document.getElementById('paymentStatusFilter').value;
  const from = document.getElementById('paymentDateFrom').value;
  const to = document.getElementById('paymentDateTo').value;
  const filtered = patientPaymentAppointments.filter(appointment => {
    const paymentStatus = String(appointment.payment_status || 'INITIATED').toUpperCase();
    const searchable = [
      appointment.id, appointment.doctor_name, appointment.consultation_type,
      appointment.payment_method, paymentStatus,
    ].join(' ').toLowerCase();
    const date = normalizeDateTime(appointment.slot_start_utc).slice(0, 10);
    return (!query || searchable.includes(query))
      && (!status || paymentStatus === status)
      && (!from || date >= from)
      && (!to || date <= to);
  });
  body.replaceChildren(...filtered.map(appointment => {
    const row = document.createElement('tr');
    const values = [
      formatDateTime(appointment.slot_start_utc),
      appointment.doctor_name || 'Doctor',
      appointment.consultation_type || 'Consultation',
      MediSlotMoney.formatINR(appointment.fee_amount || 0),
      String(appointment.payment_status || 'INITIATED').replaceAll('_', ' '),
      String(appointment.payment_method || 'PAY_AT_CLINIC').replaceAll('_', ' '),
      MediSlotMoney.formatINR(appointment.refund_amount || 0),
    ];
    values.forEach((value, index) => {
      const cell = document.createElement('td');
      cell.dataset.label = ['Appointment', 'Doctor', 'Consultation', 'Amount', 'Status', 'Method', 'Refunded'][index];
      if (index === values.length - 1) {
        const details = document.createElement('button');
        details.type = 'button';
        details.className = 'btn btn-secondary btn-sm';
        details.textContent = 'Details';
        details.addEventListener('click', () => showPaymentDetails(appointment));
        cell.appendChild(details);
      } else {
        cell.textContent = value;
      }
      row.appendChild(cell);
    });
    row.classList.add('patient-payment-row');
    row.tabIndex = 0;
    row.setAttribute('aria-label', `View payment details for appointment ${appointment.id || ''}`);
    row.addEventListener('click', event => {
      if (!event.target.closest('button')) showPaymentDetails(appointment);
    });
    row.addEventListener('keydown', event => {
      if ((event.key === 'Enter' || event.key === ' ') && !event.target.closest('button')) {
        event.preventDefault();
        showPaymentDetails(appointment);
      }
    });
    return row;
  }));
  const table = document.getElementById('patientPaymentsContainer');
  const empty = document.getElementById('patientPaymentsEmpty');
  table.hidden = filtered.length === 0;
  empty.hidden = filtered.length > 0;
}

function showPaymentDetails(appointment) {
  const body = document.getElementById('patientPaymentDetailsBody');
  if (!body) return;
  const details = [
    ['Appointment date', formatDateTime(appointment.slot_start_utc)],
    ['Appointment ID', appointment.id || '—'],
    ['Doctor', appointment.doctor_name || 'Doctor'],
    ['Hospital', [appointment.hospital_name, appointment.hospital_city].filter(Boolean).join(', ') || '—'],
    ['Consultation', appointment.consultation_type || 'Consultation'],
    ['Appointment status', String(appointment.status || 'UNKNOWN').replaceAll('_', ' ')],
    ['Amount', MediSlotMoney.formatINR(appointment.fee_amount || 0)],
    ['Payment status', String(appointment.payment_status || 'INITIATED').replaceAll('_', ' ')],
    ['Payment method', String(appointment.payment_method || 'PAY_AT_CLINIC').replaceAll('_', ' ')],
    ['Refund recorded', MediSlotMoney.formatINR(appointment.refund_amount || 0)],
  ];
  body.replaceChildren(...details.map(([label, value]) => {
    const row = document.createElement('div');
    row.className = 'patient-detail-row';
    const key = document.createElement('span');
    key.textContent = label;
    const content = document.createElement('strong');
    content.textContent = value;
    row.append(key, content);
    return row;
  }));
  document.getElementById('patientPaymentDetailsDialog').showModal();
}

async function fetchJson(url, options = {}) {
  const response = await fetch(url, options);
  let data;
  try {
    data = await response.json();
  } catch (error) {
    data = {};
  }
  if (!response.ok) {
    const error = new Error(data.message || `Request failed (${response.status})`);
    error.status = response.status;
    error.data = data;
    throw error;
  }
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