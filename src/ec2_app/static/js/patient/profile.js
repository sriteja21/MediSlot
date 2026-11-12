document.addEventListener('DOMContentLoaded', () => {
  const profileForm = document.getElementById('profileForm');
  if (!profileForm) return;

  const profileView = document.getElementById('profileView');
  const profileLoading = document.getElementById('profileLoading');
  const profileError = document.getElementById('profileError');
  const editButton = document.getElementById('editProfileButton');
  const deleteDialog = document.getElementById('deleteDialog');
  let savedProfile = null;

  const dateInput = document.getElementById('profileDob');
  dateInput.max = new Date().toISOString().slice(0, 10);

  loadProfile();
  editButton.addEventListener('click', enterEditMode);
  profileForm.addEventListener('submit', saveProfile);
  document.getElementById('cancelProfileEdit').addEventListener('click', cancelEdit);
  document.getElementById('profileNotes').addEventListener('input', updateNotesCount);
  document.getElementById('openDeleteDialog').addEventListener('click', openDeleteDialog);
  document.getElementById('cancelDelete').addEventListener('click', () => deleteDialog.close());
  document.getElementById('deleteConfirmation').addEventListener('input', event => {
    document.getElementById('confirmDelete').disabled = event.target.value !== 'DELETE';
  });
  document.getElementById('confirmDelete').addEventListener('click', deleteProfile);

  async function loadProfile() {
    profileError.hidden = true;
    MediSlotPatient.setLoading(profileLoading, true, 'Loading your profile...');
    try {
      savedProfile = await MediSlotPatient.loadPatientProfile();
      renderProfile(savedProfile);
      profileView.hidden = false;
      editButton.hidden = false;
    } catch (error) {
      MediSlotPatient.setErrorState(profileError, error.message || 'Could not load your profile.', loadProfile);
    } finally {
      MediSlotPatient.setLoading(profileLoading, false);
    }
  }

  function renderProfile(profile) {
    const values = {
      viewName: profile.name,
      viewDob: profile.date_of_birth,
      viewGender: profile.gender ? capitalize(profile.gender) : 'Not provided',
      viewPhone: profile.phone || 'Not provided',
      viewEmail: profile.email,
      viewCity: profile.city || 'Not provided',
      viewEmergencyName: profile.emergency_contact_name || 'Not provided',
      viewEmergencyPhone: profile.emergency_contact_phone || 'Not provided',
      viewNotes: profile.notes || 'No notes added.',
    };
    Object.entries(values).forEach(([id, value]) => {
      document.getElementById(id).textContent = value || 'Not provided';
    });
    const fields = {
      name: profile.name,
      date_of_birth: profile.date_of_birth,
      gender: profile.gender || '',
      phone: profile.phone || '',
      email: profile.email || '',
      city: profile.city || '',
      emergency_contact_name: profile.emergency_contact_name || '',
      emergency_contact_phone: profile.emergency_contact_phone || '',
      notes: profile.notes || '',
    };
    Object.entries(fields).forEach(([name, value]) => {
      const field = profileForm.elements.namedItem(name);
      if (field) field.value = value;
    });
    updateNotesCount();
  }

  function enterEditMode() {
    profileError.hidden = true;
    profileView.hidden = true;
    profileForm.hidden = false;
    editButton.hidden = true;
    profileForm.querySelector('input[name="name"]').focus();
  }

  function cancelEdit() {
    if (formChanged() && !window.confirm('Discard your unsaved profile changes?')) return;
    renderProfile(savedProfile);
    profileForm.hidden = true;
    profileView.hidden = false;
    editButton.hidden = false;
    clearErrors();
  }

  function formChanged() {
    return JSON.stringify(formPayload()) !== JSON.stringify(profilePayload(savedProfile));
  }

  function profilePayload(profile) {
    return {
      name: profile.name || '',
      date_of_birth: String(profile.date_of_birth || '').slice(0, 10),
      gender: profile.gender || '',
      phone: profile.phone || '',
      city: profile.city || '',
      emergency_contact_name: profile.emergency_contact_name || '',
      emergency_contact_phone: profile.emergency_contact_phone || '',
      notes: profile.notes || '',
    };
  }

  function formPayload() {
    return {
      name: document.getElementById('profileName').value.trim(),
      date_of_birth: document.getElementById('profileDob').value,
      gender: document.getElementById('profileGender').value,
      phone: document.getElementById('profilePhone').value.trim(),
      city: document.getElementById('profileCity').value.trim(),
      emergency_contact_name: document.getElementById('profileEmergencyName').value.trim(),
      emergency_contact_phone: document.getElementById('profileEmergencyPhone').value.trim(),
      notes: document.getElementById('profileNotes').value,
    };
  }

  function validate(payload) {
    const errors = {};
    if (!payload.name || payload.name.length > 150) errors.name = 'Enter your name (up to 150 characters).';
    if (!payload.date_of_birth) errors.date_of_birth = 'Date of birth is required.';
    else if (payload.date_of_birth >= new Date().toISOString().slice(0, 10)) errors.date_of_birth = 'Date of birth must be in the past.';
    if (payload.gender && !['female', 'male', 'other'].includes(payload.gender)) errors.gender = 'Choose a listed gender.';
    const phonePattern = /^\+?[0-9][0-9\s\-]{6,19}$/;
    if (payload.phone && !phonePattern.test(payload.phone)) errors.phone = 'Enter a valid phone number.';
    if (payload.emergency_contact_phone && !phonePattern.test(payload.emergency_contact_phone)) errors.emergency_contact_phone = 'Enter a valid phone number.';
    if (Boolean(payload.emergency_contact_name) !== Boolean(payload.emergency_contact_phone)) {
      errors.emergency_contact_name = 'Enter both emergency contact fields or leave both empty.';
      errors.emergency_contact_phone = 'Enter both emergency contact fields or leave both empty.';
    }
    if (payload.city.length > 100) errors.city = 'City must be 100 characters or fewer.';
    if (payload.notes.length > 2000) errors.notes = 'Notes must be 2000 characters or fewer.';
    return errors;
  }

  async function saveProfile(event) {
    event.preventDefault();
    clearErrors();
    const payload = formPayload();
    const localErrors = validate(payload);
    if (Object.keys(localErrors).length) {
      MediSlotPatient.showFieldErrors(localErrors);
      return;
    }

    const button = document.getElementById('saveProfileButton');
    MediSlotPatient.setBusy(button, true, 'Saving...');
    try {
      const result = await MediSlotPatient.api('/api/patient/me', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      savedProfile = result.profile;
      MediSlotPatient.setPatientProfile(savedProfile);
      renderProfile(savedProfile);
      profileForm.hidden = true;
      profileView.hidden = false;
      editButton.hidden = false;
      MediSlotPatient.showToast('Profile saved.', 'success');
    } catch (error) {
      MediSlotPatient.showFieldErrors(error.fieldErrors);
      MediSlotPatient.showToast(error.message || 'Could not save your profile.', 'error');
    } finally {
      MediSlotPatient.setBusy(button, false);
    }
  }

  function clearErrors() {
    MediSlotPatient.clearFieldErrors(profileForm);
    profileError.hidden = true;
  }

  function updateNotesCount() {
    const notes = document.getElementById('profileNotes').value;
    document.getElementById('notesCounter').textContent = `${notes.length} / 2000`;
  }

  function openDeleteDialog() {
    document.getElementById('deleteConfirmation').value = '';
    document.getElementById('confirmDelete').disabled = true;
    document.getElementById('deleteError').hidden = true;
    deleteDialog.showModal();
  }

  async function deleteProfile() {
    const button = document.getElementById('confirmDelete');
    const errorBox = document.getElementById('deleteError');
    button.disabled = true;
    button.textContent = 'Deleting...';
    errorBox.hidden = true;
    try {
      await MediSlotPatient.api('/api/patient/me', { method: 'DELETE' });
      window.location.assign('/');
    } catch (error) {
      errorBox.textContent = error.message || 'Could not delete this account.';
      errorBox.hidden = false;
      button.textContent = 'Delete permanently';
      button.disabled = document.getElementById('deleteConfirmation').value !== 'DELETE';
    }
  }

  function capitalize(value) {
    return String(value).charAt(0).toUpperCase() + String(value).slice(1).toLowerCase();
  }
});
