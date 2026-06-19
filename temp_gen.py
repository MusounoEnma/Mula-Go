import security
hwid = 'CF5648A2-C4C9-604B-85F5-16A72619B969'
print(f'SECRET: {security._derive_totp_secret(hwid)}')
print(f'CODE: {security.generate_current_code(hwid)}')
