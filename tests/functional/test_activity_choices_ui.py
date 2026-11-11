"""A multiple-choice activity question in chat (static/js/chat/7-activities.js):
its choices arrive as `activity_choices` (ids & texts, never the answer)
& show as buttons under the question; one tap sends a choice's id, a
pick-every-one question toggles chips & Send sends the set.

Run: make test-ui
"""

import pytest

from browser import run_probe  # tests/functional/browser.py
from test_ui_contract import make_room
from test_walk_ui import PROBE_HEAD, passed

pytestmark = pytest.mark.functional


@pytest.fixture
def client(test_app):
    return test_app.test_client()


ONE_PROBE = (
    r"""
<script>
window.addEventListener('load', async () => {"""
    + PROBE_HEAD
    + r"""
    testSocket.deliver('chat_message', {id: 7, username: 'System', content: 'Which gas do plants take in?\n\n- **a**: Carbon dioxide\n- **b**: Oxygen'});
    testSocket.deliver('activity_choices', {choices: [{id: 'a', text: 'Carbon dioxide'}, {id: 'b', text: 'Oxygen'}], multiple: false});
    const question = document.getElementById('message-7');
    const buttons = [...question.querySelectorAll('.activity-choices .followup-chip')];
    check(buttons.length === 2, 'two choices under the question: ' + buttons.length);
    check(buttons[0].textContent === 'a: Carbon dioxide', 'a choice shows its id & text: ' + buttons[0].textContent);
    check(!question.querySelector('.activity-choices-send'), 'one pick needs no Send');
    const phone = window.innerWidth <= 768;
    buttons.forEach(button => {
        const height = button.getBoundingClientRect().height;
        check(height >= (phone ? 40 : 36), 'a choice is tall enough to tap: ' + height);
    });
    check(document.documentElement.scrollWidth <= window.innerWidth + 1, 'the page does not scroll sideways');
    buttons[1].click();
    const sent = testSocket.sent.filter(([event]) => event === 'chat_message').pop();
    check(sent && sent[1].message === 'b', 'a tap sends its id: ' + (sent && sent[1].message));
    check(!document.querySelector('.activity-choices'), 'the choices go once one is picked');
    finish();
});
</script>
"""
)

ALL_PROBE = (
    r"""
<script>
window.addEventListener('load', async () => {"""
    + PROBE_HEAD
    + r"""
    testSocket.deliver('chat_message', {id: 8, username: 'System', content: 'Pick every primary colour of light.'});
    testSocket.deliver('activity_choices', {choices: [{id: 'r', text: 'Red'}, {id: 'g', text: 'Green'}, {id: 'b', text: 'Blue'}, {id: 'y', text: 'Yellow'}], multiple: true});
    const row = document.querySelector('#message-8 .activity-choices');
    const chips = [...row.querySelectorAll('.followup-chip')];
    const send = row.querySelector('.activity-choices-send');
    check(chips.length === 4 && send, 'four chips & a Send');
    check(send.disabled, 'Send waits for a pick');
    chips[2].click(); chips[0].click(); chips[1].click(); chips[1].click(); chips[1].click();
    check(chips[0].getAttribute('aria-pressed') === 'true', 'a picked chip says so');
    check(chips[3].getAttribute('aria-pressed') === 'false', 'an unpicked one says so');
    check(!send.disabled, 'Send is ready once one is picked');
    send.click();
    const sent = testSocket.sent.filter(([event]) => event === 'chat_message').pop();
    check(sent && sent[1].message === 'r, g, b', 'Send sends the picks in their order: ' + (sent && sent[1].message));
    // A typed answer instead moves past them too.
    testSocket.deliver('chat_message', {id: 9, username: 'System', content: 'Next: which is a mammal?'});
    testSocket.deliver('activity_choices', {choices: [{id: 'x', text: 'Whale'}, {id: 'y', text: 'Shark'}], multiple: false});
    testSocket.deliver('chat_message', {id: 10, username: 'ada', content: 'x'});
    check(!document.querySelector('.activity-choices'), 'someone answering clears the choices');
    finish();
});
</script>
"""
)


@pytest.mark.parametrize("width", [320, 1280])
def test_one_pick_choices_send_an_id(client, tmp_path, width):
    make_room("quiz", message="hi")
    html = client.get("/chat/quiz").get_data(as_text=True)
    passed(run_probe(html, ONE_PROBE, tmp_path, width))


def test_pick_every_one_sends_the_set(client, tmp_path):
    make_room("quiz", message="hi")
    html = client.get("/chat/quiz").get_data(as_text=True)
    passed(run_probe(html, ALL_PROBE, tmp_path, 375))
