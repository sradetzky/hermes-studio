function choiceControl(question, choice, name) {
  const label = document.createElement('label');
  label.className = 'interaction-choice';
  const input = document.createElement('input');
  input.type = question.multi_select ? 'checkbox' : 'radio';
  input.name = name;
  input.value = choice;
  input.dataset.choice = 'true';
  label.append(input, document.createTextNode(choice));
  return label;
}

function otherControl(question, name) {
  const row = document.createElement('div');
  row.className = 'interaction-other';
  let toggle = null;
  if (!question.multi_select) {
    toggle = document.createElement('input');
    toggle.type = 'radio';
    toggle.name = name;
    toggle.value = '__other__';
    toggle.dataset.otherToggle = 'true';
    const label = document.createElement('label');
    label.className = 'interaction-choice';
    label.append(toggle, document.createTextNode('Other'));
    row.append(label);
  } else {
    const label = document.createElement('label');
    label.textContent = 'Other';
    label.className = 'interaction-other-label';
    row.append(label);
  }
  const text = document.createElement('input');
  text.type = 'text';
  text.maxLength = 10000;
  text.className = 'interaction-text';
  text.placeholder = 'Type another answer…';
  text.dataset.otherText = 'true';
  if (toggle) text.addEventListener('input', () => {
    if (text.value) toggle.checked = true;
  });
  row.append(text);
  return row;
}

function questionField(question, interactionId) {
  const fieldset = document.createElement('fieldset');
  fieldset.className = 'interaction-question';
  fieldset.dataset.questionId = question.id;
  const legend = document.createElement('legend');
  legend.textContent = question.question;
  fieldset.append(legend);
  if (question.choices.length) {
    const choices = document.createElement('div');
    choices.className = 'interaction-choices';
    const name = `interaction-${interactionId}-${question.id}`;
    for (const choice of question.choices) {
      choices.append(choiceControl(question, choice, name));
    }
    choices.append(otherControl(question, name));
    fieldset.append(choices);
  } else {
    const text = document.createElement('textarea');
    text.rows = 3;
    text.maxLength = 10000;
    text.className = 'interaction-text interaction-free-text';
    text.dataset.freeText = 'true';
    text.placeholder = 'Type your answer…';
    fieldset.append(text);
  }
  return fieldset;
}

function collectAnswers(form, interaction) {
  const answers = {};
  for (const question of interaction.questions) {
    const field = form.querySelector(
      `[data-question-id="${CSS.escape(question.id)}"]`);
    if (!field) throw new Error('The question changed. Refresh and try again.');
    const freeText = field.querySelector('[data-free-text]');
    if (freeText) {
      const value = freeText.value.trim();
      if (!value) throw new Error('Answer every question before continuing.');
      answers[question.id] = value;
      continue;
    }
    const other = field.querySelector('[data-other-text]').value.trim();
    if (question.multi_select) {
      const values = [...field.querySelectorAll('[data-choice]:checked')]
        .map(input => input.value);
      if (other) values.push(other);
      if (!values.length) throw new Error('Answer every question before continuing.');
      answers[question.id] = values;
      continue;
    }
    const selected = field.querySelector('input[type="radio"]:checked');
    if (!selected || (selected.dataset.otherToggle && !other)) {
      throw new Error('Answer every question before continuing.');
    }
    answers[question.id] = selected.dataset.otherToggle ? other : selected.value;
  }
  return answers;
}

export function clearInteractionPanel() {
  const panel = document.querySelector('#interaction-panel');
  if (!panel) return;
  panel.hidden = true;
  panel.replaceChildren();
  delete panel.dataset.signature;
}

export function renderInteractionPanel(interaction, onSubmit) {
  const panel = document.querySelector('#interaction-panel');
  if (!interaction) {
    clearInteractionPanel();
    return;
  }
  const signature = `${interaction.id}:${interaction.revision}:${interaction.status}`;
  if (panel.dataset.signature === signature) return;
  panel.dataset.signature = signature;
  panel.hidden = false;
  panel.replaceChildren();

  const heading = document.createElement('div');
  heading.className = 'interaction-heading';
  heading.textContent = interaction.status === 'answered'
    ? 'Answer submitted' : 'Studio needs your input';
  const status = document.createElement('div');
  status.className = 'interaction-status';
  status.setAttribute('role', 'status');
  status.setAttribute('aria-live', 'polite');
  if (interaction.status === 'answered') {
    status.textContent = 'Studio is continuing…';
    panel.append(heading, status);
    return;
  }

  const form = document.createElement('form');
  form.className = 'interaction-form';
  for (const question of interaction.questions) {
    form.append(questionField(question, interaction.id));
  }
  const actions = document.createElement('div');
  actions.className = 'interaction-actions';
  const submit = document.createElement('button');
  submit.type = 'submit';
  submit.className = 'btn';
  submit.textContent = 'Continue Studio';
  actions.append(status, submit);
  form.append(actions);
  form.addEventListener('submit', async event => {
    event.preventDefault();
    let answers;
    try {
      answers = collectAnswers(form, interaction);
    } catch (error) {
      status.textContent = error.message;
      return;
    }
    for (const control of form.elements) control.disabled = true;
    status.textContent = 'Submitting answer…';
    try {
      await onSubmit(answers);
    } catch (error) {
      for (const control of form.elements) control.disabled = false;
      status.textContent = `Could not submit: ${error.message}`;
    }
  });
  panel.append(heading, form);
}
