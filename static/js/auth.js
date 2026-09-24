// Email-code sign in for templates/_auth_steps.html, on auth.html & in
// chat's sign-in dialog. What success does comes from data-after on
// #auth-flow: "reload" (chat) or a same-site path to go to.

let pendingEmail = '';

function showAuthStep(step) {
    document.querySelectorAll('.auth-step').forEach(el => el.classList.remove('active'));
    document.getElementById('auth-step-' + step).classList.add('active');
    clearAuthErrors();
    const input = document.querySelector('#auth-step-' + step + ' input');
    if (input) input.focus();
}

function clearAuthErrors() {
    document.querySelectorAll('.auth-step .error-message').forEach(el => el.textContent = '');
}

function backToEmailStep() {
    showAuthStep('email');
}

async function postAuth(url, body) {
    const response = await fetch(url, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(body)
    });
    return [response, await response.json()];
}

function showSignedIn(user) {
    document.getElementById('auth-success-name').textContent = user.display_name;
    showAuthStep('success');
}

async function sendOTP() {
    const email = document.getElementById('auth-email').value.trim();
    const errorEl = document.getElementById('auth-email-error');
    if (!email) {
        errorEl.textContent = 'Please enter your email';
        return;
    }
    try {
        const [response, data] = await postAuth('/auth/send-otp', {email});
        if (!response.ok) {
            errorEl.textContent = data.error || 'Failed to send code';
            return;
        }
        pendingEmail = email;
        document.getElementById('auth-email-display').textContent = email;
        // Say where the code really went: a server without mail writes it
        // to its own log instead, & a learner should not wait on an inbox.
        const note = document.getElementById('auth-otp-note');
        if (data.delivery === 'console') {
            note.textContent = 'This server has no email configured, so the code was written to its log. Ask whoever runs it for the code.';
            note.hidden = false;
        } else {
            note.textContent = '';
            note.hidden = true;
        }
        showAuthStep('otp');
    } catch (error) {
        errorEl.textContent = 'Network error. Please try again.';
    }
}

async function verifyOTP() {
    const otpCode = document.getElementById('auth-otp').value.trim();
    const errorEl = document.getElementById('auth-otp-error');
    if (!otpCode) {
        errorEl.textContent = 'Please enter the code';
        return;
    }
    try {
        const [response, data] = await postAuth('/auth/verify-otp', {email: pendingEmail, otp_code: otpCode});
        if (!response.ok) {
            errorEl.textContent = data.error || 'Invalid code';
        } else if (data.needs_display_name) {
            showAuthStep('name');
        } else {
            showSignedIn(data.user);
        }
    } catch (error) {
        errorEl.textContent = 'Network error. Please try again.';
    }
}

async function claimName() {
    const displayName = document.getElementById('auth-display-name').value.trim();
    const errorEl = document.getElementById('auth-name-error');
    if (!displayName) {
        errorEl.textContent = 'Please enter a display name';
        return;
    }
    try {
        const [response, data] = await postAuth('/auth/claim-name', {display_name: displayName});
        if (response.ok) {
            showSignedIn(data.user);
        } else {
            errorEl.textContent = data.error || 'Failed to claim name';
        }
    } catch (error) {
        errorEl.textContent = 'Network error. Please try again.';
    }
}

function finishAuth() {
    const flow = document.getElementById('auth-flow');
    const after = (flow && flow.dataset.after) || '/';
    if (after === 'reload') {
        window.location.reload();
    } else {
        window.location.href = after;
    }
}

// Enter submits each step.
document.addEventListener('DOMContentLoaded', () => {
    const submitOnEnter = (id, action) => {
        document.getElementById(id).addEventListener('keypress', event => {
            if (event.key === 'Enter') action();
        });
    };
    submitOnEnter('auth-email', sendOTP);
    submitOnEnter('auth-otp', verifyOTP);
    submitOnEnter('auth-display-name', claimName);
});
