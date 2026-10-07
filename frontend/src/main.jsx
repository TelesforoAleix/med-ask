import React from 'react'
import { createRoot } from 'react-dom/client'
import './style.css'

function App() {
  return (
    <main>
      <header>
        <p className="eyebrow">YOUR TEXTBOOKS, WITH CONTEXT</p>
        <h1>med-ask</h1>
        <p className="intro">Start with a question.</p>
      </header>
      <form onSubmit={(event) => event.preventDefault()} role="search">
        <label htmlFor="question">What would you like to understand?</label>
        <div className="search-row">
          <input
            id="question"
            name="question"
            type="search"
            placeholder="Ask a question about your textbooks"
            aria-describedby="search-note"
          />
          <button type="submit">Search</button>
        </div>
        <p id="search-note" className="note">Search is not connected yet.</p>
      </form>
      <footer className="note">
        <a href="https://github.com/TelesforoAleix/med-ask">Source code · AGPL-3.0</a>
      </footer>
    </main>
  )
}

createRoot(document.getElementById('root')).render(<App />)
