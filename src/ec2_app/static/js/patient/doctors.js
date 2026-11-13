document.addEventListener('DOMContentLoaded', () => {
  const form = document.getElementById('doctorSearchForm');
  if (!form) return;

  const filterNames = ['name', 'hospital', 'specialisation', 'city'];
  const query = new URLSearchParams(window.location.search);
  let page = positiveInt(query.get('page'), 1);
  let pageSize = positiveInt(query.get('page_size'), 10);
  let total = 0;
  let debounceTimer;
  let searchController;

  readState(query);
  page = positiveInt(query.get('page'), page);
  pageSize = positiveInt(query.get('page_size'), pageSize);
  document.getElementById('doctorPageSize').value = String(pageSize);

  form.addEventListener('submit', event => {
    event.preventDefault();
    page = 1;
    navigate(true);
  });
  document.getElementById('clearDoctorFilters').addEventListener('click', clearFilters);
  document.getElementById('emptyClearFilters').addEventListener('click', clearFilters);
  document.getElementById('previousDoctors').addEventListener('click', () => changePage(page - 1));
  document.getElementById('nextDoctors').addEventListener('click', () => changePage(page + 1));
  document.getElementById('doctorPageSize').addEventListener('change', event => {
    pageSize = positiveInt(event.target.value, 10);
    page = 1;
    navigate(true);
  });
  document.getElementById('doctorSort').addEventListener('change', () => {
    page = 1;
    navigate(true);
  });
  ['doctorNameFilter', 'doctorHospitalFilter', 'doctorSpecialisationFilter', 'doctorCityFilter'].forEach(id => {
    const field = document.getElementById(id);
    field.addEventListener('input', () => {
      window.clearTimeout(debounceTimer);
      debounceTimer = window.setTimeout(() => { page = 1; navigate(false); }, 400);
    });
    field.addEventListener('keydown', event => {
      if (event.key === 'Enter') {
        window.clearTimeout(debounceTimer);
        event.preventDefault();
        page = 1;
        navigate(true);
      }
    });
  });
  window.addEventListener('popstate', () => {
    const current = new URLSearchParams(window.location.search);
    readState(current);
    page = positiveInt(current.get('page'), 1);
    pageSize = positiveInt(current.get('page_size'), 10);
    document.getElementById('doctorPageSize').value = String(pageSize);
    loadDoctors();
  });

  loadOptions().finally(() => {
    navigate(false);
  });

  function positiveInt(value, fallback) {
    const parsed = Number.parseInt(value || '', 10);
    return Number.isInteger(parsed) && parsed > 0 ? parsed : fallback;
  }

  function readState(searchParams) {
    document.getElementById('doctorNameFilter').value = searchParams.get('name') || '';
    document.getElementById('doctorHospitalFilter').value = searchParams.get('hospital') || '';
    document.getElementById('doctorSpecialisationFilter').value = searchParams.get('specialisation') || '';
    document.getElementById('doctorCityFilter').value = searchParams.get('city') || '';
    document.getElementById('doctorSort').value = searchParams.get('sort') || 'relevance';
  }

  function stateParams() {
    const params = new URLSearchParams();
    filterNames.forEach(name => {
      const value = document.querySelector(`[name="${name}"]`).value.trim();
      if (value) params.set(name, value);
    });
    const sort = document.getElementById('doctorSort').value;
    if (sort && sort !== 'relevance') params.set('sort', sort);
    params.set('page', String(page));
    params.set('page_size', String(pageSize));
    return params;
  }

  function navigate(push) {
    const params = stateParams();
    const queryString = params.toString();
    const url = `${window.location.pathname}${queryString ? `?${queryString}` : ''}`;
    if (push) window.history.pushState(null, '', url);
    else window.history.replaceState(null, '', url);
    loadDoctors();
  }

  async function loadOptions() {
    const error = document.getElementById('doctorOptionsError');
    error.hidden = true;
    try {
      const options = await MediSlotPatient.api('/api/doctors/options');
      populateDatalist('doctorSpecialisations', options.specialisations || []);
      populateDatalist('doctorCities', options.cities || []);
      renderHospitals(options.hospitals || []);
      const pageSizeSelect = document.getElementById('doctorPageSize');
      const configuredMax = Number(options.max_page_size);
      if (!Number.isInteger(configuredMax) || configuredMax < 1) {
        throw new Error('Doctor search configuration is unavailable.');
      }
      const sizes = [10, 20, 50].filter(size => size <= configuredMax);
      if (!sizes.includes(configuredMax)) sizes.push(configuredMax);
      pageSizeSelect.replaceChildren(...sizes.map(size => new Option(String(size), String(size))));
      if (pageSize > configuredMax) pageSize = configuredMax;
      pageSizeSelect.value = String(pageSize);
    } catch (errorValue) {
      MediSlotPatient.setErrorState(error, errorValue.message || 'Could not load filter options.', loadOptions);
    }
  }

  function populateDatalist(id, values) {
    const list = document.getElementById(id);
    list.replaceChildren(...values.map(value => {
      const option = document.createElement('option');
      option.value = value;
      return option;
    }));
  }

  function renderHospitals(hospitals) {
    const container = document.getElementById('hospitalBrowseList');
    container.replaceChildren();
    if (!hospitals.length) {
      document.getElementById('hospitalBrowse').hidden = true;
      return;
    }

    hospitals.forEach(hospital => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'hospital-browse-card';
      const name = document.createElement('strong');
      name.textContent = hospital.name;
      const location = document.createElement('span');
      location.textContent = hospital.city || 'Location not listed';
      button.append(name, location);
      button.addEventListener('click', () => {
        document.getElementById('doctorHospitalFilter').value = hospital.name;
        page = 1;
        navigate(true);
        document.getElementById('doctorResultsSection').scrollIntoView({
          behavior: 'smooth',
          block: 'start',
        });
      });
      container.appendChild(button);
    });
  }

  async function loadDoctors() {
    const loading = document.getElementById('doctorSearchLoading');
    const error = document.getElementById('doctorSearchError');
    const empty = document.getElementById('doctorEmptyState');
    const results = document.getElementById('doctorResults');
    const pagination = document.getElementById('doctorPagination');
    const searchButton = document.getElementById('doctorSearchButton');
    searchController?.abort();
    const controller = new AbortController();
    searchController = controller;
    error.hidden = true;
    empty.hidden = true;
    pagination.hidden = true;
    results.replaceChildren();
    renderFilterChips();
    MediSlotPatient.setLoading(loading, true, 'Finding doctors...');
    MediSlotPatient.setBusy(searchButton, true, 'Searching...');

    try {
      const data = await MediSlotPatient.api(`/api/doctors?${stateParams()}`, {
        signal: controller.signal,
      });
      const items = data.items || [];
      total = Number(data.total || 0);
      page = Number(data.page || page);
      pageSize = Number(data.page_size || pageSize);
      renderSummary();
      if (!items.length) {
        empty.hidden = false;
        return;
      }
      items.forEach(item => results.appendChild(createDoctorCard(item)));
      renderPagination();
    } catch (errorValue) {
      if (controller.signal.aborted) return;
      MediSlotPatient.setErrorState(error, errorValue.message || 'Could not load doctors.', loadDoctors);
    } finally {
      if (searchController === controller) {
        searchController = null;
        MediSlotPatient.setLoading(loading, false);
        MediSlotPatient.setBusy(searchButton, false);
      }
    }
  }

  function renderSummary() {
    const start = total ? (page - 1) * pageSize + 1 : 0;
    const end = Math.min(page * pageSize, total);
    document.getElementById('doctorResultSummary').textContent = total
      ? `Showing ${start}-${end} of ${total} doctors`
      : 'No doctors found';
  }

  function createDoctorCard(doctor) {
    const card = document.createElement('a');
    card.className = 'doctor-card';
    const from = `${window.location.pathname}${window.location.search}`;
    card.href = `/patient/doctors/${encodeURIComponent(doctor.doctor_id)}?from=${encodeURIComponent(from)}`;

    const identity = document.createElement('div');
    identity.className = 'doctor-card-identity';
    const avatar = document.createElement('span');
    avatar.className = 'doctor-avatar';
    avatar.setAttribute('aria-hidden', 'true');
    avatar.textContent = String(doctor.name || 'D').split(/\s+/).slice(0, 2).map(part => part[0]).join('').toUpperCase();
    if (doctor.photo_url) {
      const photo = document.createElement('img');
      photo.className = 'doctor-card-photo';
      photo.src = doctor.photo_url;
      photo.alt = '';
      photo.addEventListener('error', () => {
        photo.remove();
        avatar.classList.remove('doctor-avatar-fallback');
      });
      identity.append(photo);
      avatar.classList.add('doctor-avatar-fallback');
    }
    identity.append(avatar);

    const details = document.createElement('div');
    details.className = 'doctor-card-details';
    const heading = document.createElement('h2');
    heading.textContent = doctor.name || 'Doctor';
    const specialisation = document.createElement('p');
    specialisation.className = 'doctor-card-specialisation';
    specialisation.textContent = (doctor.specialisation || []).join(', ') || 'General practice';
    const facts = document.createElement('p');
    facts.className = 'doctor-card-facts';
    facts.textContent = [doctor.qualifications, `${Number(doctor.years_of_experience || 0)} years experience`, doctor.hospital, doctor.city].filter(Boolean).join(' · ');
    const languages = document.createElement('div');
    languages.className = 'doctor-card-tags';
    (doctor.languages || []).forEach(language => languages.append(MediSlotPatient.createTag(language)));
    details.append(heading, specialisation, facts, languages);

    const consultationList = document.createElement('div');
    consultationList.className = 'doctor-card-consultations';
    (doctor.consultations || []).forEach(consultation => {
      const item = document.createElement('span');
      item.textContent = `${consultation.name} · ${Number(consultation.duration)} min · INR ${Number(consultation.fee).toFixed(2)}`;
      consultationList.appendChild(item);
    });
    if (consultationList.childElementCount) details.appendChild(consultationList);

    const fee = document.createElement('div');
    fee.className = 'doctor-card-fee';
    fee.textContent = doctor.lowest_fee == null ? 'Fees vary' : `From INR ${Number(doctor.lowest_fee).toFixed(2)}`;
    const action = document.createElement('span');
    action.className = 'doctor-card-action';
    action.textContent = 'View profile';
    card.append(identity, details, fee, action);
    return card;
  }

  function renderFilterChips() {
    const container = document.getElementById('activeDoctorFilters');
    container.replaceChildren();
    const labels = { name: 'Name', hospital: 'Hospital', specialisation: 'Specialisation', city: 'City' };
    filterNames.forEach(name => {
      const field = document.querySelector(`[name="${name}"]`);
      const value = field.value.trim();
      if (!value) return;
      const chip = document.createElement('button');
      chip.type = 'button';
      chip.className = 'patient-filter-chip';
      chip.setAttribute('aria-label', `Remove ${labels[name]} filter`);
      chip.textContent = `${labels[name]}: ${value} ×`;
      chip.addEventListener('click', () => {
        field.value = '';
        page = 1;
        navigate(true);
      });
      container.appendChild(chip);
    });
  }

  function renderPagination() {
    const container = document.getElementById('doctorPagination');
    const pages = document.getElementById('doctorPageNumbers');
    const pageCount = Math.max(1, Math.ceil(total / pageSize));
    pages.replaceChildren();
    for (let number = 1; number <= pageCount; number++) {
      if (pageCount > 7 && Math.abs(number - page) > 2 && number !== 1 && number !== pageCount) continue;
      const button = document.createElement('button');
      button.type = 'button';
      button.className = `patient-page-number${number === page ? ' current' : ''}`;
      button.textContent = String(number);
      button.setAttribute('aria-current', number === page ? 'page' : 'false');
      button.addEventListener('click', () => changePage(number));
      pages.appendChild(button);
    }
    document.getElementById('previousDoctors').disabled = page <= 1;
    document.getElementById('nextDoctors').disabled = page >= pageCount;
    container.hidden = pageCount <= 1;
  }

  function changePage(nextPage) {
    const pageCount = Math.max(1, Math.ceil(total / pageSize));
    if (nextPage < 1 || nextPage > pageCount) return;
    page = nextPage;
    navigate(true);
    document.getElementById('doctorResultsSection').scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  function clearFilters() {
    filterNames.forEach(name => { document.querySelector(`[name="${name}"]`).value = ''; });
    document.getElementById('doctorSort').value = 'relevance';
    page = 1;
    navigate(true);
  }
});
