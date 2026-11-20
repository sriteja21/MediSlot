document.addEventListener('DOMContentLoaded', async () => {
  const name = document.getElementById('sidebarDoctorName');
  const status = document.getElementById('sidebarDoctorStatus');
  if (!name || !status) return;

  try {
    const response = await fetch('/api/doctor/profile', {
      credentials: 'same-origin',
    });
    if (!response.ok) {
      status.textContent = 'Verification status unavailable';
      return;
    }

    const profile = await response.json();
    if (profile.full_name) name.textContent = `Dr. ${profile.full_name}`;
    status.textContent = profile.is_verified
      ? 'Verified Provider'
      : 'Verification pending';
  } catch (error) {
    status.textContent = 'Verification status unavailable';
  }
});
