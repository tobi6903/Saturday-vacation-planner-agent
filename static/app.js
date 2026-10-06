/* Saturday Planner — Frontend Logic */

const CATEGORY_ICONS = {
  'Food': '🍽️', 'Cafe': '☕', 'Coffee': '☕', 'Music': '🎵', 'Live Music': '🎸',
  'Walk': '🚶', 'Park': '🌳', 'Garden': '🌿', 'Nature Walk': '🌿', 'Nature': '🌿',
  'Culture': '🏛️', 'Museum': '🏛️', 'Art Gallery': '🎨', 'Art': '🎨', 'Heritage': '🏰',
  'Shopping': '🛍️', 'Market': '🛒', 'Mall': '🏬', 'Cinema': '🎬', 'Movies': '🎬',
  'Sports': '⚽', 'Fitness': '💪', 'Adventure': '🧗', 'Transport': '🚗',
  'Activity': '🎯', 'Exploration': '🗺️', 'Food Street': '🍜',
};

function getIcon(category) {
  if (!category) return '📍';
  for (const [key, icon] of Object.entries(CATEGORY_ICONS)) {
    if (category.toLowerCase().includes(key.toLowerCase())) return icon;
  }
  return '📍';
}

// Budget slider
function updateBudget(val) {
  const v = parseInt(val);
  document.getElementById('budgetLabel').textContent = '₹' + v.toLocaleString('en-IN');
  let tag, color;
  if (v < 500) { tag = 'Low Budget'; color = 'bg-blue-100 text-blue-700'; }
  else if (v < 2000) { tag = 'Medium'; color = 'bg-green-100 text-green-700'; }
  else if (v < 5000) { tag = 'High'; color = 'bg-orange-100 text-orange-700'; }
  else { tag = 'Premium'; color = 'bg-purple-100 text-purple-700'; }
  const el = document.getElementById('budgetTag');
  el.textContent = tag;
  el.className = `ml-2 text-xs px-2 py-0.5 rounded-full ${color}`;
}

// Mood selection
let selectedMood = 'Tired but wants something fun';
function selectMood(btn) {
  document.querySelectorAll('.mood-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  selectedMood = btn.dataset.mood;
  document.getElementById('mood').value = selectedMood;
  document.getElementById('moodCustom').value = '';
}

// Initialize first mood button
document.addEventListener('DOMContentLoaded', () => {
  const firstMood = document.querySelector('.mood-btn');
  if (firstMood) firstMood.classList.add('active');
});

// Interests toggle
const selectedInterests = new Set(['walks']);
function toggleInterest(btn) {
  const val = btn.dataset.interest;
  if (selectedInterests.has(val)) {
    selectedInterests.delete(val);
    btn.classList.remove('active');
  } else {
    selectedInterests.add(val);
    btn.classList.add('active');
  }
}

// Constraints toggle
const selectedConstraints = new Set(['vegetarian', 'avoid crowded places']);
function toggleConstraint(btn) {
  const val = btn.dataset.constraint;
  if (selectedConstraints.has(val)) {
    selectedConstraints.delete(val);
    btn.classList.remove('active');
  } else {
    selectedConstraints.add(val);
    btn.classList.add('active');
  }
}

// Form submit
document.getElementById('plannerForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const mood = document.getElementById('mood').value || selectedMood;
  const payload = {
    city: document.getElementById('city').value.trim(),
    budget: parseFloat(document.getElementById('budget').value),
    available_time: document.getElementById('available_time').value,
    mood: mood,
    interests: Array.from(selectedInterests),
    constraints: Array.from(selectedConstraints),
  };

  if (!payload.city) return alert('Please enter a city.');
  if (selectedInterests.size === 0) return alert('Please select at least one interest.');

  startPlanning(payload);
});

function startPlanning(payload) {
  // Reset UI
  document.getElementById('emptyState').classList.add('hidden');
  document.getElementById('planPanel').classList.add('hidden');
  document.getElementById('tracePanel').classList.remove('hidden');
  document.getElementById('traceLog').innerHTML = '';
  document.getElementById('traceStatus').textContent = 'Running...';
  document.getElementById('traceDot').classList.add('pulse-dot');

  // Reset progress steps
  document.querySelectorAll('.progress-step').forEach(s => {
    s.classList.remove('done', 'active');
    s.style.background = '';
    s.style.color = '';
  });

  // Disable form
  const btn = document.getElementById('submitBtn');
  document.getElementById('submitText').textContent = 'Planning...';
  document.getElementById('submitIcon').textContent = '⏳';
  btn.disabled = true;
  btn.classList.add('opacity-70', 'cursor-not-allowed');

  // SSE stream
  fetchPlan(payload);
}

async function fetchPlan(payload) {
  try {
    const response = await fetch('/api/plan', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });

    if (!response.ok) {
      const err = await response.json();
      addTraceItem('error', '❌ ' + (err.detail || 'Request failed'));
      resetSubmitBtn();
      return;
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop();
      for (const line of lines) {
        if (line.startsWith('data: ')) {
          const raw = line.slice(6).trim();
          if (raw === '[DONE]') {
            finishPlanning();
            return;
          }
          try {
            const event = JSON.parse(raw);
            handleEvent(event);
          } catch (_) {}
        }
      }
    }
    finishPlanning();
  } catch (err) {
    addTraceItem('error', '❌ Connection error: ' + err.message);
    resetSubmitBtn();
  }
}

function handleEvent(event) {
  const { type, message, data, step } = event;

  if (step) updateProgressStep(step);

  switch (type) {
    case 'trace':
      addTraceItem('trace', '🔍 ' + message);
      break;
    case 'tool_result':
      addTraceItem('result', '✅ ' + message);
      break;
    case 'error':
      addTraceItem('error', '❌ ' + message);
      break;
    case 'plan':
      if (data) renderPlan(data, message);
      break;
  }
}

function updateProgressStep(step) {
  const steps = document.querySelectorAll('.progress-step');
  steps.forEach(s => {
    const n = parseInt(s.dataset.step);
    if (n < step) s.classList.add('done'), s.classList.remove('active');
    else if (n === step) s.classList.add('active'), s.classList.remove('done');
    else s.classList.remove('done', 'active');
  });
}

function addTraceItem(type, msg) {
  const log = document.getElementById('traceLog');
  const colors = { trace: 'text-gray-600', result: 'text-green-700', error: 'text-red-600' };
  const div = document.createElement('div');
  div.className = `trace-item flex gap-2 ${colors[type] || 'text-gray-600'}`;
  div.innerHTML = `<span class="text-gray-300 shrink-0">${new Date().toLocaleTimeString('en-US', {hour:'2-digit', minute:'2-digit', second:'2-digit'})}</span><span>${msg}</span>`;
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
}

function finishPlanning() {
  document.getElementById('traceStatus').textContent = 'Done ✓';
  document.getElementById('traceDot').classList.remove('pulse-dot');
  // Mark all steps done
  document.querySelectorAll('.progress-step').forEach(s => s.classList.add('done'));
  resetSubmitBtn();
}

function resetSubmitBtn() {
  const btn = document.getElementById('submitBtn');
  document.getElementById('submitText').textContent = 'Plan My Perfect Saturday';
  document.getElementById('submitIcon').textContent = '🗓️';
  btn.disabled = false;
  btn.classList.remove('opacity-70', 'cursor-not-allowed');
}

function renderPlan(plan, message) {
  document.getElementById('planPanel').classList.remove('hidden');

  // Header
  document.getElementById('planTitle').textContent = plan.title || 'Your Perfect Saturday';
  document.getElementById('planSummary').textContent = plan.summary || '';

  // Budget
  const total = plan.total_estimated_cost || 0;
  const budget = plan.budget || 1;
  const pct = Math.min(Math.round(total / budget * 100), 100);
  document.getElementById('costUsed').textContent = '₹' + total.toLocaleString('en-IN');
  document.getElementById('costTotal').textContent = '₹' + budget.toLocaleString('en-IN');
  document.getElementById('remainingBudget').textContent = `₹${(plan.remaining_budget || 0).toLocaleString('en-IN')} remaining`;
  document.getElementById('budgetPct').textContent = pct + '% used';
  setTimeout(() => {
    document.getElementById('costBar').style.width = pct + '%';
    document.getElementById('costBar').className = `cost-bar h-2 rounded-full ${pct > 100 ? 'bg-red-500' : pct > 80 ? 'bg-orange-400' : 'bg-purple-500'}`;
  }, 100);

  // Time slots
  const container = document.getElementById('timeSlots');
  container.innerHTML = '';
  (plan.time_slots || []).forEach((slot, idx) => {
    const icon = getIcon(slot.category);
    const mapsLink = slot.maps_url
      ? `<a href="${slot.maps_url}" target="_blank" rel="noopener" class="text-purple-500 hover:underline text-xs">📍 Open in Maps</a>`
      : '';
    const div = document.createElement('div');
    div.className = 'slot-line relative pl-10';
    div.innerHTML = `
      <div class="absolute left-0 top-0 w-10 h-10 rounded-full flex items-center justify-center text-lg
        ${idx === 0 ? 'bg-purple-100' : 'bg-gray-100'}">
        ${icon}
      </div>
      <div class="pb-6">
        <div class="flex items-baseline gap-2 flex-wrap">
          <span class="text-xs font-mono text-purple-600 font-medium">${slot.time}</span>
          <span class="text-sm font-semibold text-gray-900">${slot.activity}</span>
          <span class="text-xs bg-gray-100 text-gray-500 px-2 py-0.5 rounded-full">${slot.duration}</span>
          ${slot.estimated_cost > 0
            ? `<span class="text-xs font-medium text-green-700 bg-green-50 px-2 py-0.5 rounded-full">₹${slot.estimated_cost}</span>`
            : `<span class="text-xs text-gray-400">Free</span>`}
        </div>
        <p class="text-xs text-gray-500 mt-0.5">${slot.location}</p>
        <p class="text-xs text-gray-600 mt-1 italic">${slot.why}</p>
        ${slot.tips ? `<p class="text-xs text-blue-600 mt-1">💡 ${slot.tips}</p>` : ''}
        <div class="mt-1">${mapsLink}</div>
      </div>
    `;
    container.appendChild(div);
  });

  // Notes
  const hasNotes = plan.trade_offs || plan.fallback_plan || plan.agent_notes;
  if (hasNotes) {
    document.getElementById('notesPanel').classList.remove('hidden');
    const parts = [];
    if (plan.trade_offs) parts.push(`<strong>Trade-offs:</strong> ${plan.trade_offs}`);
    if (plan.agent_notes) parts.push(`<strong>Note:</strong> ${plan.agent_notes}`);
    document.getElementById('tradeOffs').innerHTML = parts.join('<br>');
    if (plan.fallback_plan) {
      document.getElementById('fallback').innerHTML = `<strong>Fallback Plan:</strong> ${plan.fallback_plan}`;
    }
  }

  // Scroll to plan
  document.getElementById('planPanel').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function resetForm() {
  document.getElementById('planPanel').classList.add('hidden');
  document.getElementById('tracePanel').classList.add('hidden');
  document.getElementById('emptyState').classList.remove('hidden');
  document.getElementById('traceLog').innerHTML = '';
  window.scrollTo({ top: 0, behavior: 'smooth' });
}
