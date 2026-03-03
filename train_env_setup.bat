@echo off
echo Setting up training environment...

:: Optional: Create a virtual environment
:: python -m venv venv
:: call venv\Scripts\activate

echo Installing required packages...
pip install pandas scikit-learn lightgbm pingouin

echo Setup complete.
pause
