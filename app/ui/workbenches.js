/* Workspace tools. All model comparisons and research synthesis run on this computer. */
const workLabel = (fr, en) => LANG === 'en' ? en : fr;
const workButton = (label, action) => {
  const node = el('button', 'wsbtn');
  node.textContent = label;
  node.onclick = async () => {
    node.disabled = true;
    try { await action(); } catch (error) { alert(error.message); }
    finally { node.disabled = false; }
  };
  return node;
};

async function workRequest(path, data) {
  const response = data ? await post(path, data) : await fetch(path);
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || response.statusText);
  return result;
}

function recipeFields(collection, task, prompt, model) {
  const title = workLabel('Comparer deux versions', 'Compare two versions');
  const form = el('div', 'wsf'), kind = el('select'), left = el('select'), right = el('select');
  kind.add(new Option(workLabel('Synthèse à valider', 'Summary for review'), 'summary'));
  kind.add(new Option(title, 'diff'));
  if (task.compare_documents?.length) kind.value = 'diff';
  const versions = el('div', 'row2');
  versions.append(field(workLabel('Ancienne version', 'Older version'), left), field(workLabel('Nouvelle version', 'Newer version'), right));
  form.append(field(workLabel('Recette', 'Recipe'), kind), versions);
  function update() {
    form.hidden = !collection.value;
    form.style.display = collection.value ? '' : 'none';
    const diff = !!collection.value && kind.value === 'diff';
    model.disabled = diff;
    prompt.disabled = diff;
    versions.style.display = diff ? '' : 'none';
    if (!diff) return;
    const docs = (WS.knowledge.find(c => String(c.id) === collection.value)?.docs || []).filter(d => d.state === 'ready');
    [left,right].forEach((select, i) => {
      const old = select.value || String(task.compare_documents?.[i] ?? '');
      select.replaceChildren(...docs.map(d => new Option(d.name, String(d.id))));
      select.value = docs.some(d => String(d.id) === old) ? old : String(docs[i]?.id ?? '');
    });
  }
  kind.onchange = update;
  return {form, update, title, selected() {
    if (!collection.value || kind.value !== 'diff') return [];
    if (!left.value || !right.value || left.value === right.value) {
      alert(workLabel('Choisis deux documents indexés différents.', 'Choose two different indexed documents.')); return null;
    }
    return [Number(left.value), Number(right.value)];
  }};
}

let researchView = null;
async function wsResearch(box) {
  const panel = el('div', 'wsf');
  box.append(panel);
  const live = () => panel.isConnected && WTAB === 'research' && $('ws').classList.contains('open');
  try {
    if (!researchView) {
      const topic = inp('textarea', '', workLabel('Question à étudier', 'Research question'));
      panel.append(field(workLabel('Sujet de recherche', 'Research topic'), topic),
        workButton(workLabel('Proposer un plan', 'Suggest a plan'), async () => {
          const row = await workRequest('/api/research', {action:'draft', question:topic.value});
          researchView = row.id;
          if (live()) renderWs();
        }));
      const list = await workRequest('/api/research');
      if (!live()) return;
      for (const row of list.reverse()) {
        panel.append(wsItem(null, row.question, researchStatus(row), [[workLabel('Ouvrir', 'Open'), () => {
          researchView = row.id; renderWs();
        }]]));
      }
      return;
    }
    const row = await workRequest('/api/research?id=' + encodeURIComponent(researchView));
    if (!live() || row.id !== researchView) return;
    panel.append(workButton(workLabel('Toutes les recherches', 'All research'), () => { researchView = null; renderWs(); }));
    const heading = el('h3');
    heading.textContent = row.question;
    panel.append(heading);
    if (row.status === 'draft') {
      const steps = inp('textarea', row.steps.join('\n'));
      panel.append(field(workLabel('Plan modifiable · une question par ligne', 'Edit plan · one question per line'), steps));
      const checks = WS.knowledge.map(c => {
        const check = inp('checkbox'), label = el('label', 'ck');
        label.append(check, document.createTextNode(c.name)); panel.append(label);
        return [check, c.id];
      });
      const hosts = inp('text', '', 'www.python.org');
      panel.append(field(workLabel('Sites autorisés (facultatif, séparés par une virgule)', 'Allowed sites (optional, comma separated)'), hosts));
      panel.append(el('p', 'wsnote', workLabel('Les questions du plan sont envoyées au moteur de recherche uniquement si tu indiques un site. Le rapport est rédigé par ton modèle local.',
        'Plan questions go to the search engine only when you enter a site. Your local model writes the report.')));
      panel.append(workButton(workLabel('Valider le plan et rechercher', 'Approve plan and research'), async () => {
        await workRequest('/api/research', {action:'start', id:row.id, steps:steps.value.split('\n').map(s => s.trim()).filter(Boolean),
          collections:checks.filter(([check]) => check.checked).map(([,id]) => id), hosts:hosts.value.split(',').map(s => s.trim()).filter(Boolean)});
        if (live()) renderWs();
      }));
    } else {
      const status = el('p'); status.textContent = researchStatus(row); panel.append(status);
      const plan = el('ol');
      row.steps.forEach((s, i) => {const li = el('li'); li.textContent = (i < row.completed ? '✓ ' : '') + s; plan.append(li);});
      panel.append(plan);
      if (row.report) {
        const report = el('div', 'md'); report.style.whiteSpace = 'pre-wrap'; report.textContent = row.report; panel.append(report);
        const refs = el('ol');
        for (const src of row.sources) {
          const li = el('li');
          if (src.url && /^https?:\/\//i.test(src.url)) {
            const link = el('a'); link.href = src.url; link.target = '_blank'; link.rel = 'noreferrer'; link.textContent = src.title; li.append(link);
          } else li.textContent = src.title;
          refs.append(li);
        }
        panel.append(refs);
      } else if (!row.active) {
        panel.append(workButton(workLabel('Reprendre', 'Resume'), async () => {await workRequest('/api/research', {action:'start', id:row.id}); if (live()) renderWs();}));
      } else wsTimer = setTimeout(() => {if (live()) renderWs();}, 1500);
    }
    panel.append(workButton(workLabel('Supprimer la recherche', 'Delete research'), async () => {
      if (!confirm(workLabel('Supprimer le plan et le rapport ?', 'Delete the plan and report?'))) return;
      await workRequest('/api/research', {action:'delete', id:row.id}); researchView = null; if (live()) renderWs();
    }));
  } catch (error) {
    if (live()) {
      panel.append(document.createTextNode(error.message), workButton(workLabel('Retour', 'Back'), () => {researchView = null; renderWs();}));
    }
  }
}

function researchStatus(row) {
  if (row.error) return row.error;
  if (row.status === 'draft') return workLabel('Plan à valider', 'Plan awaiting approval');
  if (row.status === 'done') return workLabel('Rapport terminé', 'Report ready');
  return row.completed + ' / ' + row.steps.length + ' · ' + (row.active ? workLabel('En cours', 'Running') : workLabel('En pause', 'Paused'));
}

const comparison = {sides:[], busy:false, choice:null};
function wsCompare(box) {
  const panel = el('div', 'wsf'); box.append(panel);
  const local = (WS.task_models || []).filter(model => !model.cloud);
  const redraw = () => {if (WTAB === 'compare' && $('ws').classList.contains('open')) renderWs();};
  if (local.length < 2 && !comparison.sides.length) {
    panel.append(document.createTextNode(workLabel('Installe deux modèles de chat locaux dans la bibliothèque pour les comparer.',
      'Install two local chat models in the library to compare them.'))); return;
  }
  if (!comparison.sides.length) {
    const selects = [0,1].map(i => {
      const node = el('select'); for (const m of local) node.add(new Option(m.label, m.id)); node.selectedIndex = i; return node;
    });
    const models = el('div','row2'), question = inp('textarea');
    models.append(field(workLabel('Premier modèle','First model'), selects[0]), field(workLabel('Second modèle','Second model'), selects[1]));
    panel.append(models, field(workLabel('Question commune','Shared question'), question),
      workButton(workLabel('Comparer les réponses','Compare answers'), async () => {
        const text = question.value.trim();
        if (!text || selects[0].value === selects[1].value) throw new Error(workLabel('Choisis deux modèles distincts et une question.', 'Choose two different models and enter a question.'));
        comparison.sides = selects.map(node => ({id:node.value, label:node.selectedOptions[0].text,
          messages:[{role:'user', content:text}], seconds:null, error:'', pending:true}));
        comparison.busy = true; redraw();
        try {
          for (const side of comparison.sides) {
            await compareAnswer(side); redraw();
          }
        } finally {comparison.busy = false; redraw();}
      }));
    return;
  }
  const cards = el('div');
  cards.style.cssText = 'display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,220px),1fr));gap:12px';
  for (const side of comparison.sides) {
    if (comparison.choice && comparison.choice !== side) continue;
    const card = el('section', 'wsf'), title = el('strong');
    title.textContent = side.label; card.append(title);
    for (const msg of side.messages) {
      const p = el('p'); p.style.whiteSpace = 'pre-wrap'; p.textContent = msg.content; card.append(p);
    }
    const status = el('small');
    status.textContent = side.pending ? workLabel('En attente du modèle…', 'Waiting for model…') : side.error || (side.seconds + ' s');
    card.append(status);
    if (!comparison.busy && side.messages.at(-1).role === 'assistant') {
      if (!comparison.choice) card.append(workButton(workLabel('Continuer avec ce modèle', 'Continue with this model'), () => {comparison.choice = side; redraw();}));
      else {
        const follow = inp('textarea'); card.append(field(workLabel('Message suivant', 'Next message'), follow),
          workButton(workLabel('Envoyer', 'Send'), async () => {
            if (!follow.value.trim()) return;
            side.messages.push({role:'user', content:follow.value.trim()});
            comparison.busy = side.pending = true; redraw();
            try {await compareAnswer(side);} finally {comparison.busy = false; redraw();}
          }));
      }
    } else if (!comparison.busy && side.error) {
      card.append(workButton(workLabel('Réessayer','Retry'), async () => {
        comparison.busy = side.pending = true; redraw();
        try {await compareAnswer(side);} finally {comparison.busy = false; redraw();}
      }));
    }
    cards.append(card);
  }
  panel.append(cards);
  if (!comparison.busy) panel.append(workButton(workLabel('Nouvelle comparaison', 'New comparison'), () => {
    comparison.sides = []; comparison.choice = null; redraw();
  }));
}

async function compareAnswer(side) {
  try {
    const result = await workRequest('/api/compare', {model:side.id, messages:side.messages});
    side.messages.push({role:'assistant', content:result.reply}); side.seconds = result.seconds; side.error = '';
  } catch (error) {side.error = error.message;}
  finally {side.pending = false;}
}
