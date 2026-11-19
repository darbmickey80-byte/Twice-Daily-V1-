# Setting up Twice Daily on GitHub

Your broadcasts are now prepared on GitHub's computers on a schedule, so they're ready at 6 AM and 4 PM even while your phone is off. GitHub is free for this.

## 1. Create your GitHub account and repository
1. Go to **github.com** and sign up with your **personal** email (the free plan is all you need).
2. Click **+** in the top right, then **New repository**.
3. Name it **twice-daily**, choose **Public**, check **Add a README file**, and click **Create repository**.

## 2. Upload the app
4. On a computer, unzip the download and open the **twice-daily-github** folder.
5. In your repository, click **Add file > Upload files**. Select everything inside the folder (all files and folders) and drag it onto the page. Click **Commit changes**.
6. Make sure the repository now shows a **.github** folder. If it doesn't (some computers hide folders that start with a dot):
   click **Add file > Create new file**, type `.github/workflows/broadcast.yml` as the name, paste in the contents of `setup/broadcast.yml`, and click **Commit changes**.

## 3. Add your key and turn on publishing
7. In the repository, go to **Settings > Secrets and variables > Actions > New repository secret**.
   Name: `OPENAI_API_KEY`  Secret: your OpenAI key. Click **Add secret**.
   (Optional: add `ANTHROPIC_API_KEY` the same way if you want Claude to write the summaries.)
8. Go to **Settings > Pages**. Under **Build and deployment**, set **Source** to **GitHub Actions**.
9. Open the **Actions** tab. If GitHub asks, click to enable workflows. Click **Broadcast** on the left, then **Run workflow > Run workflow**.
   The first run takes about 5 to 10 minutes. When it shows a green check, your app is live at
   `https://YOUR-USERNAME.github.io/twice-daily/`

## 4. Connect your phone
10. Create an access token at **github.com/settings/personal-access-tokens/new**:
    - Name: Twice Daily phone. Expiration: the longest offered (make a new one when it expires).
    - Repository access: **Only select repositories** > **twice-daily**.
    - Repository permissions: **Actions: Read and write** and **Contents: Read and write**.
    - Click **Generate token** and copy it.
11. On your Pixel, open `https://YOUR-USERNAME.github.io/twice-daily/` in Chrome, tap **⋮ > Add to Home screen > Install**.
12. Open the app and paste the token into **Connect to GitHub**.
13. Remove the old Twice Daily icon (the Netlify one) from your home screen.

## Good to know
- **Automatic broadcasts** start at 5 AM and 3 PM your time and are published by 6 AM and 4 PM. Turn them on or off in the app.
- **Run now** prepares a new edition any time. Opening or refreshing the app never starts a broadcast.
- Your keys are stored as encrypted GitHub secrets and are never shown on the site.
- The repository is public, so anyone who knew the address could open the site. It's marked so search engines won't list it.
