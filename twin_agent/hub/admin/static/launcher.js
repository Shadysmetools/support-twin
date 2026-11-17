// Embedded Signup launcher (v4, coexistence). Runs ONLY on the launcher origin, which has no admin session.
// It posts the Meta session info and the one-time code to this origin's /callback, with the server-minted state.
// The server exchanges the code at once; the browser never sees a token. textContent only: no HTML injection.
(function () {
  var btn = document.getElementById('go');
  var st = document.getElementById('status');
  if (!btn) return;
  var STATE = btn.dataset.state, appId = btn.dataset.appId, configId = btn.dataset.configId, graph = btn.dataset.graph;

  function post(payload) {
    payload.state = STATE;
    return fetch('/callback', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload), credentials: 'omit'
    }).then(function (r) { return r.text(); });
  }

  window.addEventListener('message', function (event) {
    if (!/^https:\/\/([a-z0-9-]+\.)?facebook\.com$/.test(event.origin)) return;
    var data;
    try { data = JSON.parse(event.data); } catch (e) { return; }
    if (!data || data.type !== 'WA_EMBEDDED_SIGNUP') return;
    var d = data.data || {};
    post({ kind: 'session', event: String(data.event || ''), waba_id: d.waba_id || null,
           phone_number_id: d.phone_number_id || null, business_id: d.business_id || null,
           current_step: d.current_step || null, error_code: d.error_code || null });
    if (data.event === 'CANCEL') st.textContent = 'Stopped before the end. You can try again.';
  });

  btn.onclick = function () {
    if (!STATE || !configId || typeof FB === 'undefined') { st.textContent = 'Not ready yet.'; return; }
    FB.login(function (response) {
      if (response && response.authResponse && response.authResponse.code) {
        st.textContent = 'Finishing...';
        post({ kind: 'code', code: response.authResponse.code }).then(function (t) { st.textContent = t; });
      } else {
        st.textContent = 'Not connected. You can try again.';
      }
    }, {
      config_id: configId, response_type: 'code', override_default_response_type: true,
      extras: { setup: {}, featureType: 'whatsapp_business_app_onboarding', sessionInfoVersion: '3' }
    });
  };

  window.fbAsyncInit = function () {
    FB.init({ appId: appId, autoLogAppEvents: true, xfbml: false, version: graph });
  };
})();
