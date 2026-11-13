document.addEventListener('DOMContentLoaded', () => {
  const page = document.querySelector('[data-patient-page="doctor-profile"]');
  if (!page) return;

  const doctorId = page.dataset.doctorId;
  const backLink = document.getElementById('doctorBackLink');
  let doctor = window.MEDI_INITIAL_DOCTOR || null;
  let selectedType = null;
  let selectedDate = localDateKey(new Date());
  let slots = [];
  let unavailableDates = new Set();
  let blackoutDates = new Set();
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
        `Availability is shown for the next ${windowDays} days. Select a week, then a date to view its times.`;
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
    renderConsultations();
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

  function renderConsultations() {
    const list = document.getElementById('consultationTypes');
    const empty = document.getElementById('consultationEmpty');
    list.replaceChildren();
    const types = doctor.consultations || [];
    if (!types.length) {
      empty.hidden = false;
      return;
    }
    empty.hidden = true;
    types.forEach((type, index) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = `consultation-type-tab${index === 0 ? ' selected' : ''}`;
      button.setAttribute('role', 'tab');
      button.setAttribute('aria-selected', String(index === 0));
      button.dataset.consultationId = String(type.id);
      const title = document.createElement('strong');
      title.textContent = type.name;
      const detail = document.createElement('span');
      detail.textContent = `${Number(type.duration)} min · INR ${Number(type.fee).toFixed(2)}`;
      button.append(title, detail);
      if (Number(type.capacity) > 1) {
        const capacity = document.createElement('small');
        capacity.textContent = `Health Camp · up to ${Number(type.capacity)} seats`;
        button.appendChild(capacity);
      }
      button.addEventListener('click', () => {
        selectedType = String(type.id);
        const selectedDateHasSlots = slots.some(slot =>
          String(slot.consultation_type.id) === selectedType
          && localDateKey(new Date(slot.start)) === selectedDate
        );
        if (!selectedDateHasSlots) {
          const firstAvailableSlot = slots.find(slot =>
            String(slot.consultation_type.id) === selectedType && slot.available
          );
          if (firstAvailableSlot) {
            selectedDate = localDateKey(new Date(firstAvailableSlot.start));
            const dateOffset = Math.round(
              (localMidnight(new Date(`${selectedDate}T12:00:00`)) - availabilityStart)
              / 86400000
            );
            availabilityWeek = Math.floor(Math.max(0, dateOffset) / 7);
          }
        }
        list.querySelectorAll('.consultation-type-tab').forEach(tab => {
          const selected = tab === button;
          tab.classList.toggle('selected', selected);
          tab.setAttribute('aria-selected', String(selected));
        });
        renderDateStrip();
        renderSlotGroups();
      });
      list.appendChild(button);
      if (index === 0) selectedType = String(type.id);
    });
  }

  async function loadSlots() {
    const loading = document.getElementById('doctorSlotsLoading');
    const error = document.getElementById('doctorSlotsError');
    const strip = document.getElementById('doctorDateStrip');
    error.hidden = true;
    strip.replaceChildren();
    MediSlotPatient.setLoading(loading, true, 'Loading availability...');
    try {
      const start = availabilityStart;
      const end = new Date(start);
      end.setDate(end.getDate() + windowDays - 1);
      const fetchedSlots = [];
      const fetchedUnavailableDates = new Set();
      const fetchedBlackoutDates = new Set();
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
        (result.unavailable_dates || []).forEach(date => fetchedUnavailableDates.add(date));
        (result.blackout_dates || []).forEach(date => fetchedBlackoutDates.add(date));
      }
      slots = fetchedSlots;
      unavailableDates = fetchedUnavailableDates;
      blackoutDates = fetchedBlackoutDates;
      renderDateStrip();
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

  function renderDateStrip() {
    const strip = document.getElementById('doctorDateStrip');
    strip.replaceChildren();
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

    for (let offset = weekStartOffset; offset < weekStartOffset + weekDays; offset++) {
      const day = new Date(availabilityStart);
      day.setDate(day.getDate() + offset);
      const key = localDateKey(day);
      const button = document.createElement('button');
      button.type = 'button';
      button.className = `doctor-date-chip${key === selectedDate ? ' selected' : ''}`;
      const daySlots = slots.filter(slot =>
        (!selectedType || String(slot.consultation_type.id) === selectedType)
        && localDateKey(new Date(slot.start)) === key
      );
      button.disabled = blackoutDates.has(key) || daySlots.length === 0;
      button.title = blackoutDates.has(key) || button.disabled ? 'Doctor unavailable' : 'View available times';
      button.setAttribute('aria-pressed', String(key === selectedDate));
      button.innerHTML = `<span>${new Intl.DateTimeFormat(undefined, { weekday: 'short', timeZone: MediSlotPatient.patientTimezone }).format(day)}</span><strong>${new Intl.DateTimeFormat(undefined, { day: 'numeric', timeZone: MediSlotPatient.patientTimezone }).format(day)}</strong>${daySlots.some(slot => slot.available) ? '<i aria-hidden="true"></i>' : ''}`;
      button.addEventListener('click', () => {
        selectedDate = key;
        availabilityWeek = Math.floor(offset / 7);
        strip.querySelectorAll('.doctor-date-chip').forEach(item => {
          const selected = item === button;
          item.classList.toggle('selected', selected);
          item.setAttribute('aria-pressed', String(selected));
        });
        renderSlotGroups();
      });
      strip.appendChild(button);
    }
  }

  function selectAvailabilityWeek(week) {
    availabilityWeek = week;
    const firstOffset = week * 7;
    const lastOffset = Math.min(firstOffset + 6, windowDays - 1);
    const firstAvailableSlot = slots.find(slot => {
      if (!slot.available
          || (selectedType && String(slot.consultation_type.id) !== selectedType)) {
        return false;
      }
      const slotDate = localDateKey(new Date(slot.start));
      const offset = Math.round(
        (localMidnight(new Date(`${slotDate}T12:00:00`)) - availabilityStart)
        / 86400000
      );
      return offset >= firstOffset && offset <= lastOffset;
    });

    if (firstAvailableSlot) {
      selectedDate = localDateKey(new Date(firstAvailableSlot.start));
    } else {
      const firstDay = new Date(availabilityStart);
      firstDay.setDate(firstDay.getDate() + firstOffset);
      selectedDate = localDateKey(firstDay);
    }

    renderDateStrip();
    renderSlotGroups();
  }

  function renderSlotGroups() {
    const container = document.getElementById('doctorSlotGroups');
    const empty = document.getElementById('doctorSlotsEmpty');
    container.replaceChildren();
    selectedSlot = null;
    document.getElementById('slotSummary').hidden = true;
    const date = new Date(`${selectedDate}T12:00:00`);
    document.getElementById('selectedSlotDate').textContent = new Intl.DateTimeFormat(undefined, {
      weekday: 'long', month: 'long', day: 'numeric', timeZone: MediSlotPatient.patientTimezone,
    }).format(date);
    const matching = slots.filter(slot => (!selectedType || String(slot.consultation_type.id) === selectedType)
      && localDateKey(new Date(slot.start)) === selectedDate)
      .sort((left, right) => new Date(left.start) - new Date(right.start));
    if (!matching.length) {
      empty.hidden = false;
      return;
    }
    empty.hidden = true;
    const groups = { Morning: [], Afternoon: [], Evening: [] };
    matching.forEach(slot => groups[dayPeriod(slot.start)].push(slot));
    Object.entries(groups).forEach(([label, items]) => {
      if (!items.length) return;
      const group = document.createElement('section');
      group.className = 'doctor-slot-group';
      const title = document.createElement('h3');
      title.textContent = label;
      const chips = document.createElement('div');
      chips.className = 'doctor-slot-chips';
      items.forEach(slot => chips.appendChild(createSlotChip(slot)));
      group.append(title, chips);
      container.appendChild(group);
    });
  }

  function createSlotChip(slot) {
    const chip = document.createElement('button');
    chip.type = 'button';
    chip.className = `doctor-slot-chip${slot.available ? '' : ' full'}`;
    chip.disabled = !slot.available;
    chip.setAttribute('aria-pressed', 'false');
    const time = document.createElement('strong');
    time.textContent = MediSlotPatient.localTime(slot.start);
    chip.appendChild(time);
    const free = Number(slot.free_seats || 0);
    if (Number(slot.capacity) > 1 || free === 0) {
      const seats = document.createElement('small');
      seats.textContent = free === 0 ? 'Full' : free === 1 ? 'Last seat' : `${free} seats left`;
      chip.appendChild(seats);
    }
    if (doctor.timezone && doctor.timezone !== MediSlotPatient.patientTimezone) {
      const doctorTime = document.createElement('small');
      doctorTime.className = 'doctor-slot-secondary-time';
      doctorTime.textContent = `Doctor ${MediSlotPatient.localTime(slot.start, doctor.timezone)}`;
      chip.appendChild(doctorTime);
    }
    chip.title = free === 0 ? 'No seats available' : `${MediSlotPatient.localDateTime(slot.start)} ${MediSlotPatient.timeZoneLabel()}`;
    if (slot.available) chip.addEventListener('click', () => selectSlot(slot, chip));
    return chip;
  }

  function selectSlot(slot, selectedButton) {
    document.querySelectorAll('.doctor-slot-chip').forEach(button => {
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

  function dayPeriod(value) {
    const hour = Number(new Intl.DateTimeFormat('en', {
      hour: 'numeric', hour12: false, timeZone: MediSlotPatient.patientTimezone,
    }).formatToParts(new Date(value)).find(part => part.type === 'hour')?.value || 0);
    return hour < 12 ? 'Morning' : hour < 17 ? 'Afternoon' : 'Evening';
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
