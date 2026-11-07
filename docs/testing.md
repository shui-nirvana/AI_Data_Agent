# Testing

The project was exercised with synthetic data on local Docker and a Vultr VM. The following are examples of the observed results:

- **Online workflow:** On the Ubuntu Vultr VM, a browser-based HTTPS run processed a 50,000-row CSV using Vultr Serverless Inference and `deepseek-v4.1-flash-1017`. The independent verifier reported 20,000 changed cells and zero mismatches. The downloaded result was checked against the approved rules.
- **Controlled repair:** On Windows local Docker, a fixed error was inserted into a copy of the model-generated code. The application diagnosed the failure, requested a repair using the actual executed source, and passed independent verification. Approval was simulated through the API.
- **Sandbox and recovery:** Selected isolation and resource checks passed on the VM. Eleven automated tests covering diagnostics, resource limits and recovery passed; the Docker API timeout and lost-response scenarios were simulated.
- **HTTPS access:** The browser accepted the certificate, and two external task requests without a token returned HTTP 401. Certificate renewal was simulated successfully, and the Nginx reload hook was tested manually.

These are scoped observations, not a security certification or a guarantee for every input. Tests using real models can incur charges; local evidence may contain private data.
