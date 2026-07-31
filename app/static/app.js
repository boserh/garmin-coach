// Global double-submit guard: any HTML form on the site disables its
// submit controls the instant it's submitted, so a slow request (most are
// backed by a Claude call) can't be fired twice by an impatient click.
// A form can opt out with data-no-busy="1".
(function () {
  'use strict';

  function busyLabel(el) {
    return el.getAttribute('data-busy-text') || 'Зачекайте…';
  }

  function lockForm(form) {
    if (form.dataset.submitting === '1') return false;
    form.dataset.submitting = '1';
    var controls = form.querySelectorAll('button:not(:disabled), input[type="submit"]:not(:disabled)');
    controls.forEach(function (el) {
      el.dataset.wasEnabled = '1';
      el.disabled = true;
      if (el.tagName === 'BUTTON' && el.querySelector('*') === null) {
        el.dataset.origText = el.textContent;
        el.textContent = busyLabel(el);
      }
    });
    return true;
  }

  function unlockForm(form) {
    form.dataset.submitting = '';
    form.querySelectorAll('[data-was-enabled="1"]').forEach(function (el) {
      el.disabled = false;
      if (el.dataset.origText !== undefined) {
        el.textContent = el.dataset.origText;
        delete el.dataset.origText;
      }
      delete el.dataset.wasEnabled;
    });
  }

  document.addEventListener('submit', function (e) {
    var form = e.target;
    if (!(form instanceof HTMLFormElement)) return;
    if (form.dataset.submitting === '1') { e.preventDefault(); return; }
    if (form.dataset.noBusy === '1') return;
    lockForm(form);
  }, true);

  // Safety net: a bfcache restore (browser back button) must not leave
  // buttons stuck disabled forever if the request never actually landed.
  window.addEventListener('pageshow', function () {
    document.querySelectorAll('form[data-submitting="1"]').forEach(unlockForm);
  });
})();
