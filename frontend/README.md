# College AI Assistant Frontend

A modern, responsive web interface for the College AI RAG (Retrieval-Augmented Generation) system, focused on undergraduate admissions and bachelor's degree programs.

## Features

- **Intuitive Chat Interface**: Clean, modern design with real-time status indicators
- **Smart Filtering**: Searchable dropdowns for majors and colleges with real-time filtering
- **Responsive Design**: Works seamlessly on desktop, tablet, and mobile devices
- **Example Questions**: Built-in help with common question patterns
- **Real-time Status**: Connection monitoring and collection information display
- **Keyboard Shortcuts**: Enter in any field to submit questions, Shift + Enter for new lines (question field only)

## Quick Start

### 1. Start the Backend API

First, ensure the FastAPI backend is running:

```bash
# From the project root directory
uvicorn college_ai.api.app:app --host 0.0.0.0 --port 8000 --reload
```

### 2. Serve the Frontend

You can serve the frontend using any static file server. Here are several options:

#### Option A: Python HTTP Server (Simple)

```bash
# Navigate to the frontend directory
cd frontend

# Python 3
python -m http.server 3000

# Python 2 (if needed)
python -m SimpleHTTPServer 3000
```

#### Option B: Node.js HTTP Server

```bash
# Install a simple server (if not already installed)
npm install -g http-server

# Navigate to frontend directory and start
cd frontend
http-server -p 3000
```

#### Option C: Live Server (VS Code Extension)

If you're using VS Code, install the "Live Server" extension and right-click on `index.html` to select "Open with Live Server".

### 3. Access the Application

Open your browser and navigate to:

- `http://localhost:3000` (or whatever port you chose)

The interface will automatically check connection to the API backend at `http://localhost:8000`.

## Usage Guide

### Basic Usage

1. **Ask Questions**: Type your college-related question in the main text area
2. **Add Filters** (optional):
   - **Major**: Use searchable dropdown to select or search for majors (e.g., "Computer Science", "Business")
   - **College**: Use searchable dropdown to select or search for colleges (e.g., "MIT", "Stanford University")
   - **Results**: Choose how many sources to retrieve (5-20)
3. **Submit**: Click the send button or press Enter in any field
4. **Review Results**: Get an AI-generated answer with cited sources

### Example Questions

Click the help button (?) in the bottom-right corner to see example questions in categories like:

- **Application Requirements**: GPA requirements, standardized tests, etc.
- **Scholarships & Financial Aid**: Merit scholarships, need-based aid, FAFSA
- **Deadlines & Dates**: Application deadlines, early decision dates

### Tips for Better Results

1. **Be Specific**: Instead of "Tell me about MIT", ask "What are the admission requirements for Computer Science at MIT?"
2. **Use Filters**: Combine major and college filters for more targeted results
   - **College Filter**: When specified, results MUST match the college (primary filter)
   - **Major Filter**: When used alone, results must match. When combined with college filter, it's optional but boosts ranking
   - **Enhanced Fuzzy Matching**: Filters handle typos, misspellings, and abbreviations
     - Typos: "Computr" → "Computer Science", "Rutgrs" → "Rutgers"
     - Abbreviations: "MIT" → "Massachusetts Institute of Technology"
     - Partial matches: "University of" → multiple universities
3. **Natural Language**: Ask questions as you would to a counselor
4. **Follow-up**: Use the sources to dive deeper into specific colleges or programs

## Architecture

### Frontend Components

- **`index.html`**: Main HTML structure with semantic markup
- **`styles.css`**: Modern CSS with responsive design and animations
- **`script.js`**: JavaScript application logic and API communication

### API Integration

The frontend communicates with the FastAPI backend using these endpoints:

- `GET /health`: Check API server status
- `GET /config`: Get collection information
- `POST /ask`: Submit questions and get RAG responses

### Key Features

- **Connection Monitoring**: Real-time status checking with visual indicators
- **Error Handling**: Graceful error messages and retry mechanisms
- **Responsive Design**: Mobile-first approach with flexible layouts
- **Accessibility**: Semantic HTML, keyboard navigation, and ARIA labels
- **Performance**: Lazy loading, efficient DOM updates, and minimal dependencies

## Customization

### Changing API URL

If your backend runs on a different port or host, modify the `apiBaseUrl` in `script.js`:

```javascript
// In script.js, line ~4
this.apiBaseUrl = "http://your-host:your-port";
```

### Styling

The CSS uses CSS custom properties (variables) for easy theming. Key variables are defined at the top of `styles.css`:

```css
:root {
  --primary-color: #667eea;
  --secondary-color: #764ba2;
  --background-gradient: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
  /* ... more variables */
}
```

### Adding Features

The `CollegeAI` class in `script.js` is designed to be extensible. You can:

- Add new API endpoints
- Implement additional filters
- Create custom result formatting
- Add new interaction patterns

## Browser Support

- Chrome/Chromium 80+
- Firefox 75+
- Safari 13+
- Edge 80+

## Troubleshooting

### API Connection Issues

1. **Check Backend**: Ensure the FastAPI server is running on port 8000
2. **CORS Issues**: The backend should be configured to allow requests from your frontend origin
3. **Firewall**: Make sure ports 8000 (backend) and 3000 (frontend) are not blocked

### Common Issues

- **Blank Results**: Check browser console for JavaScript errors
- **Styling Issues**: Ensure all CSS files are loading correctly
- **Mobile Issues**: Test responsive design by resizing browser window

### Development

For development with auto-reload:

```bash
# Terminal 1: Start backend with reload
uvicorn college_ai.api.app:app --host 0.0.0.0 --port 8000 --reload

# Terminal 2: Start frontend with live reload
cd frontend
npx live-server --port=3000
```

## License

This frontend is part of the College AI project. See the main project README for licensing information.
