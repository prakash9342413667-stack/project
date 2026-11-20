import math
import string

def caculate_entropy(password):
    """Estimiting the entropy in password"""
    size = 0
    
    if any(ch in string.ascii_lowercase for ch in password):
        size += 26
    if any(ch in string.ascii_uppercase for ch in password):
        size += 26
    if any(ch in string.digits for ch in password):
        size += 10
    if any(ch in string.punctuation for ch in password):
        size += len(string.punctuation)

    if size == 0:
        return 0

    return len(password)*math.log2(size)

def password_checker(password):
    """ Check the strength of the password"""
    score = 0
    #Length
    if len(password) >= 8:
        score += 20
    #Score calcutation
    if any(ch.isupper() for ch in password):
        score += 10
    if any(ch.islower() for ch in password):
        score += 10
    if any(ch.isdigit() for ch in password):
        score += 10
    if (ch.string.punctuation for ch in password):
        score += 10
    #Extra score
    if len(password) >= 12:
        score += 20
    if len(password) >= 16:
        score += 20
    
    score = min(score , 100)

    if score < 30:
        verdict = "Weak"
    elif score < 60:
        verdict = "Moderate"
    elif score < 80:
        verdict = "Strong"
    else:
        verdict = "Very Strong"

    return score , verdict
def main():
    #ouput terminal
    "Password Strength Checker"
    password = input("Enter the Password :")

    entropy = caculate_entropy(password)
    score , verdict = password_checker(password)

    print("\n---- Password Strength Analysis ----")
    print(f"Length : {len(password)}")
    print(f"Entropy : {entropy:.2f}bits")
    print(f"Score : {score} / 100")
    print(f"Verdict : {verdict}")

main()