// Project membership is the only setting copied to a chat. Instructions and grants stay live.
function projectMenu(button) {
  menuAt(button, menu => {
    function select(id) {
      CFG.project_space = id;
      saveCfg('project_space');
    }
    mItem(menu, (CFG.project_space ? '' : '✓ ') + esc(tr('Sans projet')), () => select(null));
    for (const project of WS.projects || []) {
      mItem(menu, (CFG.project_space === project.id ? '✓ ' : '') + esc(project.name), () => select(project.id));
    }
    menu.append(el('hr'));
    mItem(menu, esc(tr('Gérer les projets…')), () => openWs('projects'));
  });
}

async function wsProjects(container) {
  const list = el('div', 'wsl');
  const add = el('button', 'wsbtn', '+ ' + tr('Nouveau projet'));
  add.onclick = () => editProject(container, {});
  container.append(el('p', 'wsnote', tr('Créer un projet pour regrouper ses conversations, ses instructions et ses documents.')), list, add);
  const response = await fetch('/api/sessions');
  if (!response.ok) {
    list.textContent = response.statusText;
    return;
  }
  const chats = await response.json();
  if (!list.isConnected) return;
  function showChat() {
    if (busy) return false;
    $('ws').classList.remove('open');
    setBotMode(false);
    setStudioMode(false);
    if (matchMedia('(max-width:760px)').matches) setSide(false);
    return true;
  }
  for (const project of WS.projects || []) {
    const collections = WS.knowledge.filter(c => project.docs.includes(c.id)).map(c => c.name);
    const row = wsItem('📁', project.name, collections.join(' · '), [
      ['Nouveau fil', () => {
        if (!showChat()) return;
        blank(false);
        CFG.project_space = project.id;
        renderTools();
      }],
      ['Modifier', () => editProject(container, project)],
      ['Supprimer', async () => {
        if (!confirm(tr('Supprimer le projet ? Ses conversations et documents resteront disponibles séparément.'))) return;
        await wsSave({what: 'projects', action: 'delete', id: project.id});
        renderWs();
      }, 'del']
    ]);
    const links = el('div', 'docs');
    for (const chat of chats.filter(c => c.project_space === project.id)) {
      const link = el('button', 'wsbtn');
      link.textContent = chat.title;
      link.onclick = () => { if (showChat()) open_(chat.id); };
      links.append(link);
    }
    row.querySelector('.n').append(links);
    list.append(row);
  }
}

function editProject(container, project) {
  container.querySelector('.wsf')?.remove();
  const form = el('form', 'wsf');
  const name = inp('text', project.name, 'Nom');
  name.required = true;
  name.maxLength = 80;
  const instructions = inp('textarea', project.system);
  instructions.maxLength = 8000;
  const collections = el('div', 'docs');
  for (const collection of WS.knowledge) {
    const label = el('label', 'ck');
    const input = inp('checkbox');
    input.value = String(collection.id);
    input.checked = (project.docs || []).includes(collection.id);
    label.append(input, document.createTextNode(collection.name));
    collections.append(label);
  }
  const actions = el('div', 'acts');
  const cancel = el('button', 'wsbtn', tr('Annuler'));
  cancel.type = 'button';
  cancel.onclick = () => form.remove();
  const save = el('button', 'wsbtn', tr('Enregistrer'));
  actions.append(cancel, save);
  form.append(field('Nom', name), field('Instructions partagées', instructions),
    el('div', 'wsnote', tr('Collections du projet')), collections, actions);
  form.onsubmit = async event => {
    event.preventDefault();
    save.disabled = true;
    try {
      await wsSave({what: 'projects', item: {id: project.id, revision: project.revision,
        name: name.value, system: instructions.value,
        docs: [...collections.querySelectorAll('input:checked')].map(input => Number(input.value))}});
      await renderWs();
    } catch {
      save.disabled = false; // wsSave already shows the error. Keep the proposed edits visible.
    }
  };
  container.append(form);
  name.focus();
}
