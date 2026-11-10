'use strict';

(() => {
  const config = AWS_CONFIG;
  const keys = {
    verifier: 'battery.pkceVerifier',
    state: 'battery.oauthState',
    accessToken: 'battery.accessToken',
    expiresAt: 'battery.expiresAt',
  };
  const signIn = document.getElementById('sign-in');
  const signOut = document.getElementById('sign-out');
  const form = document.getElementById('cloud-upload');
  const fileInput = document.getElementById('cloud-file');
  const uploadButton = document.getElementById('upload-button');
  const status = document.getElementById('cloud-status');
  const error = document.getElementById('cloud-error');

  function base64url(bytes) {
    return btoa(String.fromCharCode(...bytes))
      .replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  }

  function randomValue() {
    return base64url(crypto.getRandomValues(new Uint8Array(32)));
  }

  function clearToken() {
    sessionStorage.removeItem(keys.accessToken);
    sessionStorage.removeItem(keys.expiresAt);
  }

  function accessToken() {
    const token = sessionStorage.getItem(keys.accessToken);
    const expiresAt = Number(sessionStorage.getItem(keys.expiresAt));
    if (!token || !Number.isFinite(expiresAt) || Date.now() >= expiresAt) {
      clearToken();
      return null;
    }
    return token;
  }

  function updateAuth() {
    const signedIn = Boolean(accessToken());
    signIn.hidden = signedIn;
    signOut.hidden = !signedIn;
    form.hidden = !signedIn;
  }

  async function beginSignIn() {
    const redirect = new URL(config.redirectUri);
    if (location.origin !== redirect.origin || location.pathname !== redirect.pathname) {
      throw new Error('Open the configured CloudFront callback URL to sign in. Local PDF generation remains available here.');
    }
    const verifier = randomValue();
    const state = randomValue();
    const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier));
    sessionStorage.setItem(keys.verifier, verifier);
    sessionStorage.setItem(keys.state, state);
    const params = new URLSearchParams({
      response_type: 'code',
      client_id: config.clientId,
      redirect_uri: config.redirectUri,
      scope: 'openid',
      state,
      code_challenge_method: 'S256',
      code_challenge: base64url(new Uint8Array(digest)),
    });
    location.assign(`${config.cognitoDomain}/oauth2/authorize?${params}`);
  }

  async function finishSignIn() {
    const params = new URLSearchParams(location.search);
    if (!params.has('code') && !params.has('error')) return;
    history.replaceState(null, '', location.pathname + location.hash);
    const expectedState = sessionStorage.getItem(keys.state);
    const verifier = sessionStorage.getItem(keys.verifier);
    sessionStorage.removeItem(keys.state);
    sessionStorage.removeItem(keys.verifier);
    if (!expectedState || !verifier || params.get('state') !== expectedState) {
      throw new Error('Sign-in could not be verified. Please try again.');
    }
    if (params.has('error')) {
      throw new Error(`Cognito sign-in failed: ${params.get('error_description') || params.get('error')}`);
    }
    const response = await fetch(`${config.cognitoDomain}/oauth2/token`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams({
        grant_type: 'authorization_code',
        client_id: config.clientId,
        code: params.get('code'),
        redirect_uri: config.redirectUri,
        code_verifier: verifier,
      }),
    });
    if (!response.ok) throw new Error('Could not complete Cognito sign-in. Please try again.');
    const tokens = await response.json();
    if (typeof tokens.access_token !== 'string' || !Number.isFinite(tokens.expires_in) || tokens.expires_in <= 0) {
      throw new Error('Cognito returned an invalid sign-in response.');
    }
    sessionStorage.setItem(keys.accessToken, tokens.access_token);
    sessionStorage.setItem(keys.expiresAt, String(Date.now() + tokens.expires_in * 1000));
    status.textContent = 'Signed in. Select a CSV to upload.';
  }

  signIn.addEventListener('click', async () => {
    error.textContent = '';
    try {
      await beginSignIn();
    } catch (err) {
      error.textContent = err.message;
    }
  });

  signOut.addEventListener('click', () => {
    clearToken();
    updateAuth();
    status.textContent = 'Local sign-in cleared. Your Cognito browser session may still be active.';
    error.textContent = '';
  });

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    error.textContent = '';
    status.textContent = '';
    const token = accessToken();
    if (!token) {
      updateAuth();
      error.textContent = 'Your sign-in has expired. Please sign in again.';
      return;
    }
    const file = fileInput.files[0];
    if (!file || file.size === 0) {
      error.textContent = 'Select a non-empty CSV file.';
      return;
    }
    uploadButton.disabled = true;
    try {
      status.textContent = 'Requesting upload URL...';
      const response = await fetch(config.uploadApiUrl, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (response.status === 401 || response.status === 403) {
        throw new Error('Upload authorization failed. Check your sign-in and API permissions.');
      }
      if (!response.ok) throw new Error(`Upload URL request failed (HTTP ${response.status}).`);
      const data = await response.json();
      if (typeof data.job_id !== 'string' || !data.job_id || typeof data.upload_url !== 'string') {
        throw new Error('The upload API returned an invalid response.');
      }
      const uploadUrl = new URL(data.upload_url);
      if (uploadUrl.protocol !== 'https:') throw new Error('The upload URL must use HTTPS.');
      status.textContent = 'Uploading CSV...';
      const upload = await fetch(uploadUrl.href, {
        method: 'PUT',
        headers: { 'Content-Type': 'text/csv' },
        body: file,
      });
      if (!upload.ok) throw new Error(`S3 upload failed (HTTP ${upload.status}).`);
      status.textContent = `Upload successful. Job ID: ${data.job_id}. Report processing is not yet available.`;
    } catch (err) {
      status.textContent = '';
      error.textContent = err.message === 'Failed to fetch'
        ? 'Network request failed. Check connectivity and the API/S3 CORS settings.'
        : err.message;
    } finally {
      uploadButton.disabled = false;
    }
  });

  updateAuth();
  finishSignIn().catch((err) => {
    clearToken();
    updateAuth();
    error.textContent = err.message;
  }).then(updateAuth);
})();
