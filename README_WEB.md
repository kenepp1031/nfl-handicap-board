# Publish the NFL board for free

This project now contains a phone-friendly Streamlit web edition in
`streamlit_app.py`. It reads `nfl_handicapping_board.db`, so the deployed board
does not depend on the desktop program or this PC staying on.

## Publish it

1. Create a public GitHub repository and upload this folder, including
   `streamlit_app.py`, `requirements.txt`, and `nfl_handicapping_board.db`.
2. Go to [Streamlit Community Cloud](https://share.streamlit.io/) and sign in.
3. Select **Create app**, select the GitHub repository, and choose
   `streamlit_app.py` as the entry point.
4. Choose an app address and deploy. Save the resulting `https://…streamlit.app`
   address in your phone browser.

Streamlit Community Cloud is free and runs the published app independently of
your PC. The app is public if the GitHub repository/app is public.

## Keeping it current

The cloud app intentionally uses the database snapshot committed to GitHub.
Run the desktop app to refresh data, then replace the database in the repository
and push the change. Streamlit Cloud redeploys automatically. This prevents a
public website from needing access to your home computer.
