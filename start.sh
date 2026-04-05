#!/bin/bash

# College AI Assistant Startup Script
# This script starts both the backend API and frontend server

set -e  # Exit on any error

echo "🎓 Starting College AI Assistant..."
echo "========================================"

# Check if we're in the right directory
if [ ! -f "requirements.txt" ] || [ ! -d "college_ai" ]; then
    echo "❌ Error: Please run this script from the project root directory"
    echo "   Expected files: requirements.txt, college_ai/"
    exit 1
fi

# Check if Python is available
if ! command -v python3 &> /dev/null; then
    echo "❌ Error: Python 3 is required but not found"
    exit 1
fi

# Check if uvicorn is available
if ! python3 -c "import uvicorn" 2>/dev/null; then
    echo "❌ Error: uvicorn is not installed. Install with: pip install uvicorn"
    exit 1
fi

# Function to cleanup background processes
cleanup() {
    echo ""
    echo "🛑 Shutting down servers..."
    if [ ! -z "$BACKEND_PID" ]; then
        kill $BACKEND_PID 2>/dev/null || true
        echo "   ✓ Backend stopped"
    fi
    if [ ! -z "$FRONTEND_PID" ]; then
        kill $FRONTEND_PID 2>/dev/null || true
        echo "   ✓ Frontend stopped"
    fi
    echo "👋 College AI Assistant stopped"
}

# Set up cleanup on script exit
trap cleanup EXIT INT TERM

# Start the backend API server
echo "🚀 Starting backend API server..."
python3 -m uvicorn college_ai.api.app:app --host 0.0.0.0 --port 8000 &
BACKEND_PID=$!

# Wait a moment for backend to start
sleep 3

# Check if backend is running
if ! kill -0 $BACKEND_PID 2>/dev/null; then
    echo "❌ Error: Backend failed to start"
    exit 1
fi

echo "   ✓ Backend running on http://localhost:8000"

# Start the frontend server
echo "🌐 Starting frontend server..."
cd frontend

# Install dependencies if needed
if [ ! -d "node_modules" ]; then
    echo "   Installing frontend dependencies..."
    npm install --silent
fi

# Use Vite dev server
echo "   Using Vite dev server..."
npx vite --port 3000 &
FRONTEND_PID=$!

cd ..

# Wait a moment for frontend to start
sleep 2

# Check if frontend is running
if ! kill -0 $FRONTEND_PID 2>/dev/null; then
    echo "❌ Error: Frontend failed to start"
    exit 1
fi

echo "   ✓ Frontend running on http://localhost:3000"

echo ""
echo "🎉 College AI Assistant is ready!"
echo "========================================"
echo "📡 Backend API: http://localhost:8000"
echo "🌐 Frontend UI: http://localhost:3000"
echo "📚 API Docs: http://localhost:8000/docs"
echo ""
echo "💡 Open http://localhost:3000 in your browser to get started"
echo "🛑 Press Ctrl+C to stop both servers"
echo ""

# Keep the script running and wait for user interrupt
wait
