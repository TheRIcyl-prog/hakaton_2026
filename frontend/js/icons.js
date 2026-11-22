// Имя иконки передаётся из констант интерфейса.
export function icon(name) {
  return `<svg class="ui-icon" aria-hidden="true" focusable="false"><use href="/icons.svg#${name}"></use></svg>`;
}
