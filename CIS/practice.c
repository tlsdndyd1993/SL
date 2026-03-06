#include <stdio.h>

int main() {
    int a = 0;
    char b = '1';
    double c = 0;
    printf("a : %d, b(가) : %c, c: %f", a, b, c);

    scanf("%d %c %lf", &a, &b, &c);
    printf("%d %c %f", a, b, c);
    return 0;
}