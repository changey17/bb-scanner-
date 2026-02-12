#!/bin/bash
# BB Scanner setup script
set -e

echo "Setting up BB Scanner..."

# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Create .env from example if it doesn't exist
if [ ! -f .env ]; then
    cp .env.example .env
    echo ""
    echo "Created .env file - please edit it with your BookieBashing credentials:"
    echo "  BB_USERNAME=your_email@example.com"
    echo "  BB_PASSWORD=your_password"
fi

echo ""
echo "Setup complete! To run the scanner:"
echo "  source .venv/bin/activate"
echo "  python main.py"
