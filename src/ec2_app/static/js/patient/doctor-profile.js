document.addEventListener('DOMContentLoaded', () => {
  const page = document.querySelector('[data-patient-page="doctor-profile"]');
  if (!page) return;

  const doctorId = page.dataset.doctorId;
  const backLink = document.getElementById('doctorBackLink');
  let doctor = window.MEDI_INITIAL_DOCTOR || null;
  const requestedType = new URLSearchParams(location.search).get('type');
  let selectedType = requestedType;
  let slots = [];
  let availabilityStart = localMidnight(new Date());
  let availabilityWeek = 0;
  let windowDays = Number(doctor?.availability_window_days);
  let maxRangeDays = Number(doctor?.max_slot_range_days);

  loadDoctor();

  async function loadDoctor() {
    const loading = document.getElementById('doctorProfileLoading');
    const error = document.getElementById('doctorProfileError');
    if (window.MEDI_INITIAL_ERROR) {
      MediSlotPatient.setErrorState(error, window.MEDI_INITIAL_ERROR, () => {
        window.location.reload();
      });
      return;
    }
    if (!doctor) MediSlotPatient.setLoading(loading, true, 'Loading doctor profile...');
    try {
      if (!doctor) {
        const result = await MediSlotPatient.api(`/api/doctors/${encodeURIComponent(doctorId)}`);
        doctor = result.doctor;
      }
      windowDays = Number(doctor.availability_window_days);
      maxRangeDays = Number(doctor.max_slot_range_days);
      if (!Number.isInteger(windowDays) || windowDays < 1
          || !Number.isInteger(maxRangeDays) || maxRangeDays < 1) {
        throw new Error('Doctor availability configuration is unavailable.');
      }
      availabilityStart = localMidnight(new Date());
      document.getElementById('availabilityRangeDescription').textContent =
        `Browse available appointments for the next ${windowDays} days. Times are shown in ${MediSlotPatient.timeZoneLabel()}.`;
      document.getElementById('previousAvailabilityWeek').addEventListener('click', () => {
        if (availabilityWeek === 0) return;
        selectAvailabilityWeek(availabilityWeek - 1);
      });
      document.getElementById('nextAvailabilityWeek').addEventListener('click', () => {
        const lastWeek = Math.floor((windowDays - 1) / 7);
        if (availabilityWeek >= lastWeek) return;
        selectAvailabilityWeek(availabilityWeek + 1);
      });
      if (backLink) {
        const from = new URLSearchParams(location.search).get('from');
        backLink.href = from && from.startsWith('/') && !from.startsWith('//') && !from.includes('\\')
          ? from : '/patient/doctors';
      }
      renderProfile();
      await loadSlots();
    } catch (errorValue) {
      MediSlotPatient.setErrorState(error, errorValue.message || 'Could not load this doctor.', loadDoctor);
    } finally {
      MediSlotPatient.setLoading(loading, false);
    }
  }

  function renderProfile() {
    const container = document.getElementById('doctorProfileContent');
    document.getElementById('doctorProfileTitle').textContent = doctor.name || 'Doctor profile';
    container.replaceChildren();

    const header = document.createElement('div');
    header.className = 'public-doctor-heading';
    const avatar = document.createElement('div');
    avatar.className = 'doctor-avatar doctor-avatar-large';
    avatar.setAttribute('aria-hidden', 'true');
    avatar.textContent = initials(doctor.name);
    if (doctor.photo_url) {
      const photo = document.createElement('img');
      photo.className = 'public-doctor-photo';
      photo.alt = '';
      photo.src = doctor.photo_url;
      avatar.classList.add('doctor-avatar-fallback');
      photo.addEventListener('error', () => {
        photo.remove();
        avatar.classList.remove('doctor-avatar-fallback');
      });
      header.appendChild(photo);
    }
    header.appendChild(avatar);
    const identity = document.createElement('div');
    const name = document.createElement('h2');
    name.textContent = doctor.name || 'Doctor';
    const specialisation = document.createElement('p');
    specialisation.className = 'doctor-card-specialisation';
    specialisation.textContent = (doctor.specialisation || []).join(', ') || 'General practice';
    const qualifications = document.createElement('p');
    qualifications.textContent = doctor.qualifications || 'Qualifications not listed';
    identity.append(name, specialisation, qualifications);
    header.appendChild(identity);
    container.appendChild(header);

    const facts = document.createElement('dl');
    facts.className = 'patient-profile-grid doctor-profile-facts';
    addFact(facts, 'Experience', `${Number(doctor.years_of_experience || 0)} years`);
    addFact(facts, 'Hospital', doctor.hospital || 'Not listed');
    addFact(facts, 'Address', doctor.hospital_address || 'Not listed');
    addFact(facts, 'City', doctor.city || 'Not listed');
    container.appendChild(facts);

    if (doctor.languages?.length) {
      const languages = document.createElement('div');
      languages.className = 'doctor-profile-languages';
      const label = document.createElement('strong');
      label.textContent = 'Languages';
      languages.append(label, ...doctor.languages.map(language => MediSlotPatient.createTag(language)));
      container.appendChild(languages);
    }
    renderConsultationSelector();
  }

  function addFact(list, label, value) {
    const wrapper = document.createElement('div');
    const term = document.createElement('dt');
    term.textContent = label;
    const detail = document.createElement('dd');
    detail.textContent = value;
    wrapper.append(term, detail);
    list.appendChild(wrapper);
  }

  function renderConsultationSelector() {
    const select = document.getElementById('slotConsultationType');
    select.replaceChildren();
    const types = doctor.consultations || [];
    if (!types.length) {
      const option = new Option('No active consultation types', '');
      option.disabled = true;
      option.selected = true;
      select.appendChild(option);
      return;
    }
    if (!types.some(type => String(type.id) === selectedType)) {
      selectedType = String(types[0].id);
    }
    types.forEach(type => {
      const option = new Option(
        `${type.name} · ${Number(type.duration)} min · INR ${Number(type.fee).toFixed(2)}`,
        String(type.id),
        false,
        String(type.id) === selectedType
      );
      select.appendChild(option);
    });
    select.addEventListener('change', () => {
      selectedType = select.value;
      const params = new URLSearchParams(location.search);
      params.set('type', selectedType);
      history.replaceState(null, '', `${location.pathname}?${params}`);
      renderSlotGroups();
    });
    document.getElementById('onlyFreeSeats').addEventListener('change', renderSlotGroups);
  }

  async function loadSlots() {
    const loading = document.getElementById('doctorSlotsLoading');
    const error = document.getElementById('doctorSlotsError');
    error.hidden = true;
    MediSlotPatient.setLoading(loading, true, 'Loading availability...');
    try {
      const start = availabilityStart;
      const fetchedSlots = [];
      for (let offset = 0; offset < windowDays; offset += maxRangeDays) {
        const rangeStart = new Date(start);
        rangeStart.setDate(rangeStart.getDate() + offset);
        const rangeEnd = new Date(rangeStart);
        rangeEnd.setDate(rangeEnd.getDate() + Math.min(maxRangeDays, windowDays - offset) - 1);
        const params = new URLSearchParams({
          from: localDateKey(rangeStart),
          to: localDateKey(rangeEnd),
        });
        const result = await MediSlotPatient.api(
          `/api/doctors/${encodeURIComponent(doctorId)}/slots?${params}`
        );
        fetchedSlots.push(...(result.slots || []));
      }
      slots = fetchedSlots;
      renderSlotGroups();
      document.getElementById('patientTimezoneLabel').textContent = `Your time: ${MediSlotPatient.timeZoneLabel()}`;
      document.getElementById('doctorTimezoneNote').textContent = doctor.timezone && doctor.timezone !== MediSlotPatient.patientTimezone
        ? `Doctor's timezone: ${doctor.timezone}` : '';
    } catch (errorValue) {
      MediSlotPatient.setErrorState(error, errorValue.message || 'Could not load availability.', loadSlots);
    } finally {
      MediSlotPatient.setLoading(loading, false);
    }
  }

  function renderWeekLabel() {
    const weekStartOffset = availabilityWeek * 7;
    const weekDays = Math.min(7, windowDays - weekStartOffset);
    const weekStart = new Date(availabilityStart);
    weekStart.setDate(weekStart.getDate() + weekStartOffset);
    const weekEnd = new Date(weekStart);
    weekEnd.setDate(weekEnd.getDate() + weekDays - 1);
    document.getElementById('availabilityWeekLabel').textContent = new Intl.DateTimeFormat(undefined, {
      month: 'short',
      day: 'numeric',
      timeZone: MediSlotPatient.patientTimezone,
    }).formatRange(weekStart, weekEnd);
    document.getElementById('previousAvailabilityWeek').disabled = availabilityWeek === 0;
    document.getElementById('nextAvailabilityWeek').disabled =
      weekStartOffset + weekDays >= windowDays;
  }

  function selectAvailabilityWeek(week) {
    availabilityWeek = week;
    renderWeekLabel();
    renderSlotGroups();
  }

  function renderSlotGroups() {
    const container = document.getElementById('doctorSlotGroups');
    const empty = document.getElementById('doctorSlotsEmpty');
    container.replaceChildren();
    document.getElementById('slotSummary').hidden = true;
    renderWeekLabel();
    const firstDayOffset = availabilityWeek * 7;
    const dayCount = Math.min(7, windowDays - firstDayOffset);
    const weekSlots = slots.filter(slot => {
      if (selectedType && String(slot.consultation_type.id) !== selectedType) return false;
      const slotDate = localDateKey(new Date(slot.start));
      const dateOffset = Math.round(
        (localMidnight(new Date(`${slotDate}T12:00:00`)) - availabilityStart) / 86400000
      );
      return dateOffset >= firstDayOffset && dateOffset < firstDayOffset + dayCount;
    }).sort((left, right) => new Date(left.start) - new Date(right.start));
    const openSlots = weekSlots.filter(slot => slot.available);
    const freeSeats = openSlots.reduce((total, slot) => total + Number(slot.free_seats || 0), 0);
    document.getElementById('openSlotCount').textContent =
      `${openSlots.length} open ${openSlots.length === 1 ? 'slot' : 'slots'}`;
    document.getElementById('freeSeatCount').textContent =
      `${freeSeats} free ${freeSeats === 1 ? 'seat' : 'seats'}`;
    const visibleSlots = document.getElementById('onlyFreeSeats').checked
      ? weekSlots.filter(slot => slot.available)
      : weekSlots;
    if (!visibleSlots.length) {
      empty.hidden = false;
      return;
    }
    empty.hidden = true;
    for (let offset = firstDayOffset; offset < firstDayOffset + dayCount; offset++) {
      const date = new Date(availabilityStart);
      date.setDate(date.getDate() + offset);
      const dateKey = localDateKey(date);
      const daySlots = visibleSlots.filter(slot => localDateKey(new Date(slot.start)) === dateKey);
      if (!daySlots.length) continue;
      const availableSeats = daySlots.reduce(
        (total, slot) => total + Number(slot.free_seats || 0),
        0
      );
      const group = document.createElement('section');
      group.className = 'patient-slot-day';
      const heading = document.createElement('div');
      heading.className = 'patient-slot-day-heading';
      const dayName = document.createElement('h3');
      dayName.textContent = new Intl.DateTimeFormat(undefined, {
        weekday: 'short', day: 'numeric', month: 'short',
        timeZone: MediSlotPatient.patientTimezone,
      }).format(date);
      const dayCountLabel = document.createElement('span');
      dayCountLabel.textContent = `${availableSeats} ${availableSeats === 1 ? 'free seat' : 'free seats'}`;
      heading.append(dayName, dayCountLabel);
      const cards = document.createElement('div');
      cards.className = 'patient-slot-cards';
      daySlots.forEach(slot => cards.appendChild(createSlotCard(slot)));
      group.append(heading, cards);
      container.appendChild(group);
    }
  }

  function createSlotCard(slot) {
    const card = document.createElement('button');
    card.type = 'button';
    card.className = `patient-slot-card${slot.available ? '' : ' full'}`;
    card.disabled = !slot.available;
    card.setAttribute('aria-pressed', 'false');
    const time = document.createElement('strong');
    time.textContent = MediSlotPatient.localTime(slot.start);
    const consultation = (doctor.consultations || []).find(
      item => String(item.id) === String(slot.consultation_type.id)
    );
    const details = document.createElement('span');
    details.textContent = `${consultation?.name || slot.consultation_type.name} · ${Number(consultation?.duration || 0)} min · INR ${Number(consultation?.fee || 0).toFixed(2)}`;
    const free = Number(slot.free_seats || 0);
    const seats = document.createElement('small');
    seats.textContent = free === 0 ? 'Full' : `${free} ${free === 1 ? 'seat' : 'seats'} free`;
    card.append(time, details, seats);
    if (doctor.timezone && doctor.timezone !== MediSlotPatient.patientTimezone) {
      const doctorTime = document.createElement('small');
      doctorTime.className = 'doctor-slot-secondary-time';
      doctorTime.textContent = `Doctor ${MediSlotPatient.localTime(slot.start, doctor.timezone)}`;
      card.appendChild(doctorTime);
    }
    card.title = free === 0 ? 'No seats available' : `${MediSlotPatient.localDateTime(slot.start)} ${MediSlotPatient.timeZoneLabel()}`;
    if (slot.available) card.addEventListener('click', () => selectSlot(slot, card));
    return card;
  }

  function selectSlot(slot, selectedButton) {
    document.querySelectorAll('.patient-slot-card').forEach(button => {
      const selected = button === selectedButton;
      button.classList.toggle('selected', selected);
      button.setAttribute('aria-pressed', String(selected));
    });
    const consultation = (doctor.consultations || []).find(item => String(item.id) === selectedType);
    document.getElementById('slotSummaryDoctor').textContent = doctor.name;
    document.getElementById('slotSummaryDetails').textContent = `${MediSlotPatient.localDateTime(slot.start)} · ${consultation?.name || slot.consultation_type.name}`;
    document.getElementById('slotSummaryFee').textContent = `INR ${Number(consultation?.fee || 0).toFixed(2)}`;
    document.getElementById('slotSummary').hidden = false;
  }

  function localDateKey(value) {
    const parts = new Intl.DateTimeFormat('en-CA', {
      timeZone: MediSlotPatient.patientTimezone,
      year: 'numeric', month: '2-digit', day: '2-digit',
    }).formatToParts(value);
    const values = Object.fromEntries(parts.map(part => [part.type, part.value]));
    return `${values.year}-${values.month}-${values.day}`;
  }

  function localMidnight(value) {
    const date = new Date(value);
    date.setHours(0, 0, 0, 0);
    return date;
  }

  function initials(value) {
    return String(value || 'D').split(/\s+/).slice(0, 2).map(part => part[0]).join('').toUpperCase();
  }
});
