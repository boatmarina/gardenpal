# GardenPal

## Deployment

After pushing changes to the feature branch, always also merge into `main` and push `main` to origin so Vercel deploys to production automatically. Do this on every deploy unless explicitly told otherwise.

```bash
git checkout main
git merge --no-ff <feature-branch>
git push -u origin main
git checkout <feature-branch>
```
